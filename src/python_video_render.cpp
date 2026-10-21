#include "python_video_render.h"

#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

#include <chrono>
#include <filesystem>
#include <limits>
#include <map>
#include <stdexcept>
#include <vector>
#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

namespace {
namespace fs = std::filesystem;

void check(int code, const char* operation)
{
    if (code >= 0) return;
    char message[AV_ERROR_MAX_STRING_SIZE];
    av_strerror(code, message, sizeof(message));
    throw std::runtime_error(std::string(operation) + ": " + message);
}

struct VideoResources {
    AVFormatContext* input = nullptr;
    AVFormatContext* output = nullptr;
    AVCodecContext* decoder = nullptr;
    AVCodecContext* encoder = nullptr;
    AVFrame* decoded = av_frame_alloc();
    AVFrame* normalized = av_frame_alloc();
    AVFrame* encoded = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    AVPacket* compressed = av_packet_alloc();
    SwsContext* to_rgb = nullptr;
    SwsContext* to_yuv = nullptr;
    SwsContext* from_rgb = nullptr;
    fs::path scratch;

    ~VideoResources()
    {
        sws_freeContext(to_rgb);
        sws_freeContext(to_yuv);
        sws_freeContext(from_rgb);
        av_frame_free(&decoded);
        av_frame_free(&normalized);
        av_frame_free(&encoded);
        av_packet_free(&packet);
        av_packet_free(&compressed);
        avcodec_free_context(&decoder);
        avcodec_free_context(&encoder);
        avformat_close_input(&input);
        if (output) {
            if (output->pb) avio_closep(&output->pb);
            avformat_free_context(output);
        }
        if (!scratch.empty()) {
            std::error_code ignored;
            fs::remove_all(scratch, ignored);
        }
    }
};
} // namespace
#endif

namespace media_format {
bool render_python_video(const std::string& input, const std::string& output,
                         const PythonProcessorOptions& options, std::string& error)
{
    error.clear();
#if !MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    error = "Python bridge is disabled; configure MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE=ON";
    return false;
#else
    try {
        std::error_code ec;
        if (fs::equivalent(input, output, ec) ||
            fs::absolute(input).lexically_normal() == fs::absolute(output).lexically_normal())
            throw std::runtime_error("Python rendering requires a separate output file");
        VideoResources r;
        if (!r.decoded || !r.normalized || !r.encoded || !r.packet || !r.compressed)
            throw std::runtime_error("Cannot allocate video buffers");
        check(avformat_open_input(&r.input, input.c_str(), nullptr, nullptr), "Open input");
        check(avformat_find_stream_info(r.input, nullptr), "Read stream information");
        const int video = av_find_best_stream(r.input, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
        check(video, "Find video stream");
        AVStream* source = r.input->streams[video];
        const AVCodec* decoder = avcodec_find_decoder(source->codecpar->codec_id);
        const AVCodec* encoder = avcodec_find_encoder_by_name("libx264");
        if (!decoder || !encoder) throw std::runtime_error("Video decoder and libx264 encoder are required");
        r.decoder = avcodec_alloc_context3(decoder);
        r.encoder = avcodec_alloc_context3(encoder);
        if (!r.decoder || !r.encoder) throw std::runtime_error("Cannot allocate video codecs");
        check(avcodec_parameters_to_context(r.decoder, source->codecpar), "Configure decoder");
        r.decoder->pkt_timebase = source->time_base;
        r.decoder->err_recognition = AV_EF_EXPLODE;
        check(avcodec_open2(r.decoder, decoder, nullptr), "Open decoder");
        const int width = r.decoder->width, height = r.decoder->height;
        if (width <= 0 || height <= 0 || width % 2 || height % 2 ||
            width > std::numeric_limits<int>::max() / 3 ||
            static_cast<uint64_t>(width) * height * 3 > options.max_frame_bytes)
            throw std::runtime_error("Video must have even, positive dimensions within the frame byte limit");
        if (source->time_base.num <= 0 || source->time_base.den <= 0)
            throw std::runtime_error("Video timestamps have no valid time base");

        // Resolve automatic presets using this file's actual geometry/rate.
        // Rebuild the options per file so batch inputs never share source hints.
        PythonProcessorOptions processor_options = options;
        rapidjson::Document config;
        config.Parse(options.config_json.c_str());
        if (config.HasParseError() || !config.IsObject())
            throw std::runtime_error("Python processor configuration must be a JSON object");
        for (const char* key : {"source_width", "source_height", "source_fps"})
            config.RemoveMember(key);
        config.AddMember("source_width", width, config.GetAllocator());
        config.AddMember("source_height", height, config.GetAllocator());
        const AVRational average_rate = source->avg_frame_rate;
        config.AddMember("source_fps", average_rate.num > 0 && average_rate.den > 0
            ? av_q2d(average_rate) : 0.0, config.GetAllocator());
        rapidjson::StringBuffer json;
        rapidjson::Writer<rapidjson::StringBuffer> writer(json);
        config.Accept(writer);
        processor_options.config_json.assign(json.GetString(), json.GetSize());
        CorePythonBridge bridge;
        if (!bridge.open(processor_options, error)) return false;

        fs::path parent = fs::absolute(output).parent_path();
        fs::create_directories(parent);
        const auto tick = std::chrono::steady_clock::now().time_since_epoch().count();
        for (int attempt = 0; attempt < 100; ++attempt) {
            auto candidate = parent / (".media_python_" + std::to_string(tick) + "_" + std::to_string(attempt));
            if (fs::create_directory(candidate)) { r.scratch = candidate; break; }
        }
        if (r.scratch.empty()) throw std::runtime_error("Cannot create rendering temporary directory");
        const fs::path rendered = r.scratch / "rendered.mp4";
        check(avformat_alloc_output_context2(&r.output, nullptr, "mp4", rendered.string().c_str()), "Create output");
        r.output->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;
        r.encoder->width = width;
        r.encoder->height = height;
        r.encoder->pix_fmt = AV_PIX_FMT_YUV420P;
        r.encoder->time_base = source->time_base;
        r.encoder->framerate = av_guess_frame_rate(r.input, source, nullptr);
        r.encoder->sample_aspect_ratio = r.decoder->sample_aspect_ratio;
        r.encoder->color_primaries = r.decoder->color_primaries;
        r.encoder->color_trc = r.decoder->color_trc;
        r.encoder->colorspace = r.decoder->colorspace;
        r.encoder->color_range = AVCOL_RANGE_MPEG;
        r.encoder->max_b_frames = 0;
#ifdef AV_CODEC_FLAG_FRAME_DURATION
        r.encoder->flags |= AV_CODEC_FLAG_FRAME_DURATION;
#endif
        if (r.output->oformat->flags & AVFMT_GLOBALHEADER)
            r.encoder->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        check(av_opt_set(r.encoder->priv_data, "preset", "veryfast", 0), "Set encoder preset");
        check(av_opt_set(r.encoder->priv_data, "crf", "23", 0), "Set encoder quality");
        check(avcodec_open2(r.encoder, encoder, nullptr), "Open encoder");
        std::vector<int> mapping(r.input->nb_streams, -1);
        AVStream* video_output = nullptr;
        for (unsigned i = 0; i < r.input->nb_streams; ++i) {
            AVStream* src = r.input->streams[i];
            const auto type = src->codecpar->codec_type;
            if (static_cast<int>(i) != video && type != AVMEDIA_TYPE_AUDIO && type != AVMEDIA_TYPE_SUBTITLE)
                continue;
            AVStream* dst = avformat_new_stream(r.output, nullptr);
            if (!dst) throw std::runtime_error("Cannot create output stream");
            mapping[i] = dst->index;
            dst->time_base = src->time_base;
            dst->disposition = src->disposition;
            check(av_dict_copy(&dst->metadata, src->metadata, 0), "Copy stream metadata");
            if (static_cast<int>(i) == video) {
                video_output = dst;
                check(avcodec_parameters_from_context(dst->codecpar, r.encoder), "Configure output video");
                dst->sample_aspect_ratio = r.encoder->sample_aspect_ratio;
            } else {
                check(avcodec_parameters_copy(dst->codecpar, src->codecpar), "Copy audio/subtitle format");
                dst->codecpar->codec_tag = 0;
            }
        }
        check(av_dict_copy(&r.output->metadata, r.input->metadata, 0), "Copy metadata");
        check(avio_open(&r.output->pb, rendered.string().c_str(), AVIO_FLAG_WRITE), "Open temporary output");
        check(avformat_write_header(r.output, nullptr), "Write MP4 header");
        r.encoded->format = r.encoder->pix_fmt;
        r.encoded->width = width;
        r.encoded->height = height;
        check(av_frame_get_buffer(r.encoded, 32), "Allocate encoder frame");
        r.normalized->format = AV_PIX_FMT_YUV420P;
        r.normalized->width = width;
        r.normalized->height = height;
        check(av_frame_get_buffer(r.normalized, 32), "Allocate normalized frame");
        std::shared_ptr<std::vector<uint8_t>> pixels;
        uint64_t index = 1;
        int64_t previous_pts = AV_NOPTS_VALUE;
        std::map<int64_t, int64_t> frame_durations;

        auto write_encoded = [&] {
            for (;;) {
                int rc = avcodec_receive_packet(r.encoder, r.compressed);
                if (rc == AVERROR(EAGAIN) || rc == AVERROR_EOF) break;
                check(rc, "Read encoded packet");
                auto duration = frame_durations.find(r.compressed->pts);
                if (duration != frame_durations.end()) {
                    r.compressed->duration = duration->second;
                    frame_durations.erase(duration);
                }
                av_packet_rescale_ts(r.compressed, r.encoder->time_base, video_output->time_base);
                r.compressed->stream_index = video_output->index;
                r.compressed->pos = -1;
                check(av_interleaved_write_frame(r.output, r.compressed), "Write encoded video");
                av_packet_unref(r.compressed);
            }
        };
        auto process_decoded = [&] {
            for (;;) {
                int rc = avcodec_receive_frame(r.decoder, r.decoded);
                if (rc == AVERROR(EAGAIN) || rc == AVERROR_EOF) break;
                check(rc, "Decode video frame");
                if (r.decoded->width != width || r.decoded->height != height || r.decoded->decode_error_flags)
                    throw std::runtime_error("Video geometry changed or a frame is corrupt");
                const int64_t pts = r.decoded->best_effort_timestamp;
                if (pts == AV_NOPTS_VALUE || (previous_pts != AV_NOPTS_VALUE && pts <= previous_pts))
                    throw std::runtime_error("Video requires increasing presentation timestamps");
                previous_pts = pts;
                if (!pixels || pixels.use_count() != 1)
                    pixels = std::make_shared<std::vector<uint8_t>>(static_cast<size_t>(width) * height * 3);
                // Inference consumes RGB converted from canonical YUV420P.
                // Skipping this intermediate conversion changes detector inputs
                // for full-range, high-bit-depth, and other source formats.
                AVFrame* inference_source = r.decoded;
                if (r.decoded->format != AV_PIX_FMT_YUV420P) {
                    r.to_yuv = sws_getCachedContext(r.to_yuv, width, height,
                        static_cast<AVPixelFormat>(r.decoded->format), width, height,
                        AV_PIX_FMT_YUV420P, SWS_BICUBIC, nullptr, nullptr, nullptr);
                    if (!r.to_yuv) throw std::runtime_error("Cannot normalize decoded pixels");
                    const int* coefficients = sws_getCoefficients(r.decoded->colorspace == AVCOL_SPC_UNSPECIFIED
                        ? SWS_CS_DEFAULT : static_cast<int>(r.decoded->colorspace));
                    const int full_range = r.decoded->color_range == AVCOL_RANGE_JPEG;
                    // Format normalization preserves the decoder's range. The
                    // model's subsequent YUV420P-to-RGB conversion uses swscale
                    // defaults, as it does in the graph processing pipeline.
                    check(sws_setColorspaceDetails(r.to_yuv, coefficients, full_range,
                        coefficients, full_range, 0, 1 << 16, 1 << 16), "Normalize YUV range");
                    if (sws_scale(r.to_yuv, r.decoded->data, r.decoded->linesize, 0, height,
                                  r.normalized->data, r.normalized->linesize) != height)
                        throw std::runtime_error("Incomplete YUV normalization");
                    inference_source = r.normalized;
                }
                r.to_rgb = sws_getCachedContext(r.to_rgb, width, height, AV_PIX_FMT_YUV420P,
                    width, height, AV_PIX_FMT_RGB24, SWS_BICUBIC, nullptr, nullptr, nullptr);
                r.from_rgb = sws_getCachedContext(r.from_rgb, width, height, AV_PIX_FMT_RGB24,
                    width, height, r.encoder->pix_fmt, SWS_BILINEAR, nullptr, nullptr, nullptr);
                if (!r.to_rgb || !r.from_rgb) throw std::runtime_error("Cannot create RGB converter");
                uint8_t* rgb_planes[] = {pixels->data(), nullptr, nullptr, nullptr};
                int rgb_strides[] = {width * 3, 0, 0, 0};
                if (sws_scale(r.to_rgb, inference_source->data, inference_source->linesize, 0, height, rgb_planes, rgb_strides) != height)
                    throw std::runtime_error("Incomplete RGB conversion");
                PythonVideoFrame frame;
                frame.data = pixels->data(); frame.size = pixels->size(); frame.owner = pixels;
                frame.width = width; frame.height = height; frame.stride = width * 3;
                frame.frame_index = index;
                frame.pts_us = av_rescale_q(pts, source->time_base, AVRational{1, 1000000});
                PythonFrameResult result;
                if (!bridge.process(frame, result) || !result.valid || result.blocked)
                    throw std::runtime_error("Python processing failed at frame " + std::to_string(index) + ": " +
                        (result.error.empty() ? result.json : result.error));
                check(av_frame_make_writable(r.encoded), "Prepare encoder frame");
                if (sws_scale(r.from_rgb, rgb_planes, rgb_strides, 0, height, r.encoded->data, r.encoded->linesize) != height)
                    throw std::runtime_error("Incomplete encoder conversion");
                r.encoded->pts = pts;
#if LIBAVUTIL_VERSION_MAJOR >= 58
                r.encoded->duration = r.decoded->duration;
                int64_t duration = r.decoded->duration;
#else
                r.encoded->pkt_duration = r.decoded->pkt_duration;
                int64_t duration = r.decoded->pkt_duration;
#endif
                if (duration <= 0 && r.encoder->framerate.num > 0 && r.encoder->framerate.den > 0)
                    duration = av_rescale_q(1, av_inv_q(r.encoder->framerate), r.encoder->time_base);
                // Some encoder versions omit packet duration, which otherwise
                // makes the final MP4 sample zero-length and drops its frame.
                frame_durations[pts] = duration > 0 ? duration : 1;
                check(avcodec_send_frame(r.encoder, r.encoded), "Encode processed frame");
                write_encoded();
                av_frame_unref(r.decoded);
                ++index;
            }
        };
        int rc;
        while ((rc = av_read_frame(r.input, r.packet)) >= 0) {
            const int stream = r.packet->stream_index;
            if (r.packet->flags & AV_PKT_FLAG_CORRUPT)
                throw std::runtime_error("Corrupt input packet");
            if (stream == video) {
                check(avcodec_send_packet(r.decoder, r.packet), "Submit video packet");
                process_decoded();
            } else if (mapping[stream] >= 0) {
                AVStream* dst = r.output->streams[mapping[stream]];
                av_packet_rescale_ts(r.packet, r.input->streams[stream]->time_base, dst->time_base);
                r.packet->stream_index = dst->index;
                r.packet->pos = -1;
                check(av_interleaved_write_frame(r.output, r.packet), "Copy audio/subtitle packet");
            }
            av_packet_unref(r.packet);
        }
        if (rc != AVERROR_EOF) check(rc, "Read input packet");
        check(avcodec_send_packet(r.decoder, nullptr), "Flush decoder");
        process_decoded();
        if (index == 1) throw std::runtime_error("Input contains no decoded video frames");
        check(avcodec_send_frame(r.encoder, nullptr), "Flush encoder");
        write_encoded();
        check(av_write_trailer(r.output), "Finish MP4");
        check(avio_closep(&r.output->pb), "Close MP4");
        if (!bridge.close(error)) return false;
        fs::rename(rendered, output);
        return true;
    } catch (const std::exception& exception) {
        error = exception.what();
        return false;
    }
#endif
}
} // namespace media_format
