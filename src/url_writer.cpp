extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libavutil/audio_fifo.h>
#include <libswresample/swresample.h>
}

// avcodec_get_supported_config / AV_CODEC_CONFIG_SAMPLE_FORMAT were added in
// FFmpeg 7.0 (libavcodec major 61).  Older builds (e.g. Cygwin FFmpeg 6.x)
// expose the same information via the codec's sample_fmts array instead.
#ifndef FFMPEG_V7
#if LIBAVCODEC_VERSION_MAJOR >= 61
#define FFMPEG_V7 1
#endif
#endif

#include "url_writer.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <filesystem>
namespace fs = std::filesystem;
namespace media_format {

static void log_av(const char* msg, int rc) {
    char buf[256];
    av_strerror(rc, buf, sizeof(buf));
    fprintf(stderr, "%s: %s\n", msg, buf);
}

static void write_be16(uint8_t* p, uint16_t v)
{
    p[0] = (uint8_t)((v >> 8) & 0xFF);
    p[1] = (uint8_t)(v & 0xFF);
}

static void build_tx3g_default_style(uint8_t* out, size_t n,
                                     const SubtitleStyle& style)
{
    memset(out, 0, n);
    if (n < 30) return;

    // 4 bytes display flags (0), then h/v justification as signed int8.
    // Horizontal: center (0). Vertical: top(-1) or bottom(+1).
    out[4] = 0;
    out[5] = (style.position == SubtitlePosition::Top) ? (uint8_t)0xFF : (uint8_t)0x01;

    // StyleRecord at offset 18.
    write_be16(out + 18, 0);   // startChar
    write_be16(out + 20, 0);   // endChar (sample-description default)
    write_be16(out + 22, 1);   // font-ID
    out[24] = 0;               // face-style-flags
    out[25] = 18;              // font-size
    out[26] = style.r;         // text-color-rgba
    out[27] = style.g;
    out[28] = style.b;
    out[29] = style.a;
}

// Encode one AVFrame (or nullptr to flush) and write output packets to o_ctx.
// PTS on input frames is in units of {1, sample_rate}.
static void encode_audio(AVCodecContext* enc, AVFrame* frame,
                         AVFormatContext* o_ctx, AVStream* st)
{
    int rc = avcodec_send_frame(enc, frame);
    if (rc < 0) { log_av("avcodec_send_frame", rc); return; }
    AVPacket *pkt = av_packet_alloc();
    while (avcodec_receive_packet(enc, pkt) == 0) {
        pkt->stream_index = st->index;
        av_packet_rescale_ts(pkt, {1, enc->sample_rate}, st->time_base);
        av_write_frame(o_ctx, pkt);
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);
}

// Drain samples from the FIFO into the AAC encoder.
// flush=true drains any partial remainder at the end of the stream.
static void drain_fifo(AVAudioFifo* fifo, AVCodecContext* enc,
                       int64_t& a_pts, AVFormatContext* o_ctx, AVStream* st,
                       bool flush)
{
    while (av_audio_fifo_size(fifo) >= (flush ? 1 : enc->frame_size)) {
        int n = std::min(av_audio_fifo_size(fifo), enc->frame_size);
        AVFrame *f = av_frame_alloc();
        f->format     = enc->sample_fmt;
        f->nb_samples = n;
        f->pts        = a_pts;
        av_channel_layout_copy(&f->ch_layout, &enc->ch_layout);
        av_frame_get_buffer(f, 0);
        av_audio_fifo_read(fifo, (void**)f->data, n);
        a_pts += n;
        encode_audio(enc, f, o_ctx, st);
        av_frame_free(&f);
    }
}

int UrlWriter::mux(const std::string& video_tmp,
                   const std::string& audio_tmp,
                   const std::string& url,
                   uint32_t video_fps,
                   uint32_t audio_sample_rate,
                   uint32_t audio_channels,
                   bool     delete_temps,
                   const std::vector<TelemetryEvent>& events,
                   const std::vector<TelemetryEvent>& raw_events,
                   const SubtitleStyle& subtitle_style)
{
    av_log_set_level(AV_LOG_WARNING);

    AVFormatContext *v_ctx   = nullptr;
    AVFormatContext *a_ctx   = nullptr;
    AVFormatContext *o_ctx   = nullptr;
    AVCodecContext  *aac_enc = nullptr;
    SwrContext      *swr     = nullptr;
    AVAudioFifo     *fifo    = nullptr;
    AVStream        *v_st    = nullptr;
    AVStream        *a_st    = nullptr;
    AVStream        *s_st    = nullptr;
    AVStream        *s_raw_st = nullptr;
    bool             has_audio     = false;
    bool             has_subs      = !events.empty();
    // Raw telemetry extraction remains in parser/main, but we intentionally
    // do not mux it as a subtitle stream.
    bool             has_raw_subs  = false;
    bool             opened_output = false;
    int              result        = 1;
    int              rc;

    // ---- Open video input — let avformat auto-detect H264 vs H265 ----
    rc = avformat_open_input(&v_ctx, video_tmp.c_str(), nullptr, nullptr);
    if (rc < 0) { log_av("Cannot open video file", rc); goto done; }
    avformat_find_stream_info(v_ctx, nullptr);
    if (v_ctx->nb_streams == 0) {
        fprintf(stderr, "No streams found in %s\n", video_tmp.c_str());
        goto done;
    }
    {
        AVCodecID cid = v_ctx->streams[0]->codecpar->codec_id;
        fprintf(stderr, "Detected codec: %s\n", avcodec_get_name(cid));
        if (cid != AV_CODEC_ID_H264 && cid != AV_CODEC_ID_HEVC) {
            fprintf(stderr, "Unsupported video codec (expected H264 or H265)\n");
            goto done;
        }
    }
    if (video_fps > 0) {
        v_ctx->streams[0]->r_frame_rate   = {(int)video_fps, 1};
        v_ctx->streams[0]->avg_frame_rate = {(int)video_fps, 1};
    }

    // ---- Open audio input (raw PCM s16le) ----
    has_audio = !audio_tmp.empty() && fs::exists(audio_tmp);
    if (has_audio) {
        int sr = audio_sample_rate ? audio_sample_rate : 8000;
        int ch = audio_channels    ? audio_channels    : 1;
        AVDictionary *opts = nullptr;
        av_dict_set_int(&opts, "sample_rate", sr, 0);
        av_dict_set_int(&opts, "channels",    ch, 0);
        rc = avformat_open_input(&a_ctx, audio_tmp.c_str(),
                                  av_find_input_format("s16le"), &opts);
        av_dict_free(&opts);
        if (rc < 0) {
            fprintf(stderr, "Warning: cannot open audio temp file; muxing without audio\n");
            has_audio = false;
        } else {
            avformat_find_stream_info(a_ctx, nullptr);
        }
    }

    // ---- Create output context (format auto-detected from url extension) ----
    rc = avformat_alloc_output_context2(&o_ctx, nullptr, nullptr, url.c_str());
    if (rc < 0 || !o_ctx) { log_av("Cannot create output context", rc); goto done; }

    // ---- Add video stream ----
    v_st = avformat_new_stream(o_ctx, nullptr);
    if (!v_st) { fprintf(stderr, "Cannot add video stream\n"); goto done; }
    avcodec_parameters_copy(v_st->codecpar, v_ctx->streams[0]->codecpar);
    v_st->codecpar->codec_tag = 0;  // let the muxer choose
    v_st->time_base = video_fps > 0 ? AVRational{1, (int)video_fps}
                                    : v_ctx->streams[0]->time_base;
    if (video_fps > 0)
        v_st->r_frame_rate = v_st->avg_frame_rate = {(int)video_fps, 1};

    // ---- Add audio stream with AAC encoder ----
    if (has_audio) {
        int sr = audio_sample_rate ? audio_sample_rate : 8000;
        int ch = audio_channels    ? audio_channels    : 1;
        const AVCodec *aac_codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
        if (!aac_codec) {
            fprintf(stderr, "AAC encoder not found; muxing without audio\n");
            has_audio = false;
        }
        if (has_audio) {
            aac_enc = avcodec_alloc_context3(aac_codec);
            {
#ifdef FFMPEG_V7
                const enum AVSampleFormat *fmts = nullptr;
                int n = 0;
                avcodec_get_supported_config(nullptr, aac_codec,
                    AV_CODEC_CONFIG_SAMPLE_FORMAT, 0, (const void**)&fmts, &n);
                aac_enc->sample_fmt = (fmts && n > 0) ? fmts[0] : AV_SAMPLE_FMT_FLTP;
#else
                // FFmpeg < 7: sample_fmts is a NULL-terminated AVSampleFormat array on the codec struct.
                aac_enc->sample_fmt = (aac_codec->sample_fmts &&
                                       aac_codec->sample_fmts[0] != AV_SAMPLE_FMT_NONE)
                                    ? aac_codec->sample_fmts[0] : AV_SAMPLE_FMT_FLTP;
#endif
            }
            aac_enc->sample_rate = sr;
            aac_enc->bit_rate    = 128000;
            av_channel_layout_default(&aac_enc->ch_layout, ch);
            if (o_ctx->oformat->flags & AVFMT_GLOBALHEADER)
                aac_enc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
            rc = avcodec_open2(aac_enc, aac_codec, nullptr);
            if (rc < 0) { log_av("Cannot open AAC encoder", rc); has_audio = false; }
        }
        if (has_audio) {
            a_st = avformat_new_stream(o_ctx, nullptr);
            avcodec_parameters_from_context(a_st->codecpar, aac_enc);
            a_st->time_base = {1, sr};
            rc = swr_alloc_set_opts2(&swr,
                &aac_enc->ch_layout, aac_enc->sample_fmt, sr,
                &aac_enc->ch_layout, AV_SAMPLE_FMT_S16,  sr,
                0, nullptr);
            if (rc < 0 || swr_init(swr) < 0) {
                fprintf(stderr, "Cannot init resampler; muxing without audio\n");
                has_audio = false;
            }
        }
        if (has_audio) {
            fifo = av_audio_fifo_alloc(aac_enc->sample_fmt, ch,
                                       aac_enc->frame_size * 4);
            if (!fifo) has_audio = false;
        }
    }

    // ---- Add subtitle stream (timed-text / mov_text) ----
    // mov_text is a passthrough format: each subtitle packet is just
    //   [u16 big-endian length][UTF-8 text bytes]
    // The muxer doesn't need an encoder context; we write the bytes directly
    // on a stream whose codecpar declares AV_CODEC_ID_MOV_TEXT.
    if (has_subs) {
        s_st = avformat_new_stream(o_ctx, nullptr);
        if (!s_st) {
            fprintf(stderr, "Cannot add subtitle stream; continuing without subs\n");
            has_subs = false;
        } else {
            s_st->codecpar->codec_type = AVMEDIA_TYPE_SUBTITLE;
            s_st->codecpar->codec_id   = AV_CODEC_ID_MOV_TEXT;
            s_st->codecpar->codec_tag  = MKTAG('t','x','3','g');
            s_st->time_base            = AVRational{1, 1000};   // ms ticks
            // Minimal tx3g defaults (display flags/justification + style record).
            uint8_t tx3g_extra[33];
            build_tx3g_default_style(tx3g_extra, sizeof(tx3g_extra), subtitle_style);
            s_st->codecpar->extradata =
                (uint8_t*)av_mallocz(sizeof(tx3g_extra) + AV_INPUT_BUFFER_PADDING_SIZE);
            if (s_st->codecpar->extradata) {
                memcpy(s_st->codecpar->extradata, tx3g_extra, sizeof(tx3g_extra));
                s_st->codecpar->extradata_size = (int)sizeof(tx3g_extra);
            }
        }
    }
    if (has_raw_subs) {
        s_raw_st = avformat_new_stream(o_ctx, nullptr);
        if (!s_raw_st) {
            fprintf(stderr, "Cannot add raw telemetry subtitle stream; continuing without it\n");
            has_raw_subs = false;
        } else {
            s_raw_st->codecpar->codec_type = AVMEDIA_TYPE_SUBTITLE;
            s_raw_st->codecpar->codec_id   = AV_CODEC_ID_MOV_TEXT;
            s_raw_st->codecpar->codec_tag  = MKTAG('t','x','3','g');
            s_raw_st->time_base            = AVRational{1, 1000};
            uint8_t tx3g_extra[33];
            build_tx3g_default_style(tx3g_extra, sizeof(tx3g_extra), subtitle_style);
            s_raw_st->codecpar->extradata =
                (uint8_t*)av_mallocz(sizeof(tx3g_extra) + AV_INPUT_BUFFER_PADDING_SIZE);
            if (s_raw_st->codecpar->extradata) {
                memcpy(s_raw_st->codecpar->extradata, tx3g_extra, sizeof(tx3g_extra));
                s_raw_st->codecpar->extradata_size = (int)sizeof(tx3g_extra);
            }
            av_dict_set(&s_raw_st->metadata, "title", "TelemetryRaw", 0);
        }
    }

    // ---- Open output and write header ----
    // Set movflags=+faststart only for MP4/MOV/3GP containers.
    if (!(o_ctx->oformat->flags & AVFMT_NOFILE)) {
        rc = avio_open2(&o_ctx->pb, url.c_str(), AVIO_FLAG_WRITE, nullptr, nullptr);
        if (rc < 0) { log_av("Cannot open output URL", rc); goto done; }
        opened_output = true;
    }
    {
        AVDictionary *opts = nullptr;
        if (o_ctx->oformat && o_ctx->oformat->name) {
            std::string fmt = o_ctx->oformat->name;
            if (fmt == "mp4" || fmt == "mov" || fmt == "3gp" || fmt == "3g2")
                av_dict_set(&opts, "movflags", "+faststart", 0);
        }
        rc = avformat_write_header(o_ctx, &opts);
        av_dict_free(&opts);
        if (rc < 0) { log_av("Cannot write output header", rc); goto done; }
    }
    // After avformat_write_header, v_st->time_base is finalised by the muxer.

    // ---- Write video packets ----
    {
        // Use a synthetic monotonic counter rather than trusting the demuxer's
        // timestamps — raw Annex-B demuxers often produce non-monotonic or
        // AV_NOPTS_VALUE DTS that cause muxer warnings.
        AVRational frame_tb = (video_fps > 0) ? AVRational{1, (int)video_fps}
                                              : v_ctx->streams[0]->time_base;
        AVRational out_tb   = v_st->time_base;
        int64_t    v_pts    = 0;
        AVPacket  *pkt      = av_packet_alloc();
        while (av_read_frame(v_ctx, pkt) == 0) {
            pkt->stream_index = v_st->index;
            pkt->pts          = v_pts;
            pkt->dts          = v_pts;
            pkt->duration     = 1;
            ++v_pts;
            av_packet_rescale_ts(pkt, frame_tb, out_tb);
            av_write_frame(o_ctx, pkt);
            av_packet_unref(pkt);
        }
        av_packet_free(&pkt);
    }

    // ---- Write subtitle (mov_text) packets ----
    if (has_subs && s_st) {
        // Convert video_frame_idx → seconds using the same fps the video
        // stream was opened with.  Fall back to the demuxer's avg_frame_rate
        // if no fps was supplied; finally to 25 fps.
        double fps = 0.0;
        if (video_fps > 0) {
            fps = (double)video_fps;
        } else {
            AVRational r = v_ctx->streams[0]->avg_frame_rate;
            if (r.num > 0 && r.den > 0) fps = (double)r.num / r.den;
        }
        if (fps <= 0.0) fps = 25.0;

        AVPacket *pkt = av_packet_alloc();
        for (const auto& ev : events) {
            // mov_text body: u16 big-endian length, then UTF-8 text bytes.
            // Strip a trailing newline if present — players add their own.
            std::string text = ev.text;
            while (!text.empty() && (text.back() == '\n' || text.back() == '\r'))
                text.pop_back();
            const size_t tlen = text.size();
            if (tlen > 0xFFFF) continue;

            rc = av_new_packet(pkt, 2 + (int)tlen);
            if (rc < 0) continue;
            pkt->data[0] = (uint8_t)((tlen >> 8) & 0xFF);
            pkt->data[1] = (uint8_t)( tlen       & 0xFF);
            if (tlen) memcpy(pkt->data + 2, text.data(), tlen);

            int64_t pts_ms = (int64_t)((double)ev.video_frame_idx * 1000.0 / fps);
            int64_t dur_ms = (int64_t)(ev.duration_s * 1000.0);
            if (dur_ms <= 0) dur_ms = 3000;
            pkt->stream_index = s_st->index;
            pkt->pts          = pts_ms;
            pkt->dts          = pts_ms;
            pkt->duration     = dur_ms;
            av_packet_rescale_ts(pkt, AVRational{1, 1000}, s_st->time_base);
            av_write_frame(o_ctx, pkt);
            av_packet_unref(pkt);
        }
        av_packet_free(&pkt);
        fprintf(stderr, "Subtitle: %zu telemetry cue(s) written.\n", events.size());
    }
    if (has_raw_subs && s_raw_st) {
        double fps = 0.0;
        if (video_fps > 0) {
            fps = (double)video_fps;
        } else {
            AVRational r = v_ctx->streams[0]->avg_frame_rate;
            if (r.num > 0 && r.den > 0) fps = (double)r.num / r.den;
        }
        if (fps <= 0.0) fps = 25.0;

        AVPacket *pkt = av_packet_alloc();
        for (const auto& ev : raw_events) {
            std::string text = ev.text;
            while (!text.empty() && (text.back() == '\n' || text.back() == '\r'))
                text.pop_back();
            const size_t tlen = text.size();
            if (tlen == 0 || tlen > 0xFFFF) continue;
            rc = av_new_packet(pkt, 2 + (int)tlen);
            if (rc < 0) continue;
            pkt->data[0] = (uint8_t)((tlen >> 8) & 0xFF);
            pkt->data[1] = (uint8_t)( tlen       & 0xFF);
            memcpy(pkt->data + 2, text.data(), tlen);

            int64_t pts_ms = (int64_t)((double)ev.video_frame_idx * 1000.0 / fps);
            int64_t dur_ms = (int64_t)(ev.duration_s * 1000.0);
            if (dur_ms <= 0) dur_ms = 3000;
            pkt->stream_index = s_raw_st->index;
            pkt->pts          = pts_ms;
            pkt->dts          = pts_ms;
            pkt->duration     = dur_ms;
            av_packet_rescale_ts(pkt, AVRational{1, 1000}, s_raw_st->time_base);
            av_write_frame(o_ctx, pkt);
            av_packet_unref(pkt);
        }
        av_packet_free(&pkt);
        fprintf(stderr, "Subtitle: %zu raw telemetry cue(s) written.\n", raw_events.size());
    }

    // ---- Write audio packets: s16le PCM → AAC ----
    if (has_audio && a_ctx && aac_enc && swr && fifo) {
        int sr = audio_sample_rate ? audio_sample_rate : 8000;
        int ch = audio_channels    ? audio_channels    : 1;
        int bytes_per_sample = 2 * ch;
        int64_t a_pts = 0;

        AVPacket *pkt = av_packet_alloc();
        while (av_read_frame(a_ctx, pkt) == 0) {
            int n = pkt->size / bytes_per_sample;
            if (n <= 0) { av_packet_unref(pkt); continue; }

            AVFrame *in_f  = av_frame_alloc();
            in_f->format      = AV_SAMPLE_FMT_S16;
            in_f->sample_rate = sr;
            in_f->nb_samples  = n;
            av_channel_layout_default(&in_f->ch_layout, ch);
            av_frame_get_buffer(in_f, 0);
            memcpy(in_f->data[0], pkt->data, pkt->size);

            AVFrame *out_f = av_frame_alloc();
            out_f->format      = aac_enc->sample_fmt;
            out_f->sample_rate = sr;
            out_f->nb_samples  = n;
            av_channel_layout_copy(&out_f->ch_layout, &aac_enc->ch_layout);
            av_frame_get_buffer(out_f, 0);

            swr_convert(swr, out_f->data, n, (const uint8_t**)in_f->data, n);
            av_audio_fifo_write(fifo, (void**)out_f->data, n);

            av_frame_free(&in_f);
            av_frame_free(&out_f);
            av_packet_unref(pkt);

            drain_fifo(fifo, aac_enc, a_pts, o_ctx, a_st, /*flush=*/false);
        }
        av_packet_free(&pkt);

        drain_fifo(fifo, aac_enc, a_pts, o_ctx, a_st, /*flush=*/true);
        encode_audio(aac_enc, nullptr, o_ctx, a_st); // flush encoder
    }

    av_write_trailer(o_ctx);
    result = 0;
    fprintf(stderr, "Muxed to: %s  [%s]\n",
            url.c_str(),
            o_ctx->oformat ? o_ctx->oformat->long_name : "unknown format");

done:
    if (fifo)    av_audio_fifo_free(fifo);
    if (swr)     swr_free(&swr);
    if (aac_enc) avcodec_free_context(&aac_enc);
    if (v_ctx)   avformat_close_input(&v_ctx);
    if (a_ctx)   avformat_close_input(&a_ctx);
    if (o_ctx) {
        if (opened_output) avio_closep(&o_ctx->pb);
        avformat_free_context(o_ctx);
    }
    if (delete_temps) {
        std::error_code ec;
        fs::remove(video_tmp, ec);
        if (!audio_tmp.empty()) fs::remove(audio_tmp, ec);
    }
    return result;
}

} // namespace media_format
