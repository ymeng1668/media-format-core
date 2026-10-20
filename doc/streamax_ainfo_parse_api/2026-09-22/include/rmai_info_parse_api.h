/**
 * @file rmai_info_parse_api.h
 * @brief RMAI AI帧头解析接口
 * @author dpwen@streamax.com
 * @date 2025-11-12
 * @version 1.0.0
 * 
 * 说明：
 *   本接口提供 AI 帧头解析实例生命周期管理及 Struct 解析入口
 *   - 支持多路并发（句柄模式）
 *   - 线程安全
 *   - Struct 数据协议见 rmai_info_parse_struct_protocol.h
 */

#ifndef _RMAI_INFO_PARSE_API_H_
#define _RMAI_INFO_PARSE_API_H_

#include "rmai_info_parse_struct_protocol.h"

#if defined(_WIN32) || defined(_WIN64)
    #ifdef RMAI_INFO_PARSE_EXPORTS
        #define RMAI_INFO_PARSE_API __declspec(dllexport)
    #else
        #define RMAI_INFO_PARSE_API __declspec(dllimport)
    #endif
#else
    #define RMAI_INFO_PARSE_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/************************************************
 * 类型定义
 ************************************************/

/**
 * 句柄类型（数值型）
 * 每个句柄对应一路视频的解析实例
 */
typedef int RMAI_HANDLE;
#define RMAI_INVALID_HANDLE -1

/**
 * 图像尺寸
 */
typedef struct {
    int width;      ///< 图像宽度
    int height;     ///< 图像高度
    char reserved[56]; ///< 保留字段
} RMAI_ImageSize;

/**
 * 内存块
 */
typedef struct {
    unsigned long long aiPts;  ///< ai叠加内存段的时间戳pts
    unsigned char* data;  ///< 数据指针
    int size;             ///< 数据大小（字节）
    char reserved[48];    ///< 保留字段
} RMAI_Memory;

/************************************************
 * 核心接口
 ************************************************/

/**
 * @brief 创建解析实例
 * 
 * @param imageSize 图像尺寸
 * @return 句柄ID，失败返回 RMAI_INVALID_HANDLE
 * 
 * @note 线程安全：是
 * @note 说明：每路视频创建一个实例
 * 
 * @example
 *   RMAI_ImageSize size = {1920, 1080, {0}};
 *   RMAI_HANDLE handle = RMAIInfo_Parse_Create(size);
 *   if (handle == RMAI_INVALID_HANDLE) {
 *       // 创建失败
 *   }
 */
RMAI_INFO_PARSE_API RMAI_HANDLE RMAIInfo_Parse_Create(RMAI_ImageSize imageSize);

/**
 * @brief 清空解析状态（切换视频时调用）
 * 
 * @param handle 实例句柄
 * @return 0-成功，-1-失败
 * 
 * @note 线程安全：是
 * @note 说明：切换视频流时调用，清除内部的历史报警状态
 */
RMAI_INFO_PARSE_API int RMAIInfo_Parse_Clear(RMAI_HANDLE handle);

/**
 * @brief 销毁实例
 * 
 * @param handle 实例句柄
 * @return 0-成功，-1-失败
 * 
 * @note 线程安全：是
 * @note 说明：每个Create必须对应一个Destroy
 */
RMAI_INFO_PARSE_API int RMAIInfo_Parse_Destroy(RMAI_HANDLE handle);

/************************************************
 * 辅助接口
 ************************************************/

/**
 * @brief 检查句柄是否有效
 * 
 * @param handle 实例句柄
 * @return 1-有效，0-无效
 */
RMAI_INFO_PARSE_API int RMAIInfo_Parse_IsValid(RMAI_HANDLE handle);

/**
 * @brief 获取当前实例数量
 * 
 * @return 实例数量
 * @note 用于调试和监控
 */
RMAI_INFO_PARSE_API int RMAIInfo_Parse_GetInstanceCount(void);

/**
 * @brief 获取库版本信息
 * 
 * @return 版本字符串（如 "1.0.0"）
 * @note 用于技术支持和故障排查
 * 
 * @example
 *   const char* version = RMAIInfo_Parse_GetVersion();
 *   printf("RMAIInfo_Parse version: %s\n", version);
 */
RMAI_INFO_PARSE_API const char* RMAIInfo_Parse_GetVersion(void);

/************************************************
 * Struct 输出接口
 ************************************************/

/**
 * @brief 解析帧头并输出 C 变长结构体 buffer
 *
 * @param handle 实例句柄
 * @param memory 帧头数据
 * @param out 输出 buffer 指针与长度
 * @return 0-成功，-1-失败
 *
 * @note 输出布局见 rmai_info_parse_struct_protocol.h
 */
RMAI_INFO_PARSE_API int RMAIInfo_ParseStruct(
    RMAI_HANDLE handle,
    RMAI_Memory memory,
    RMAI_StructOutput* out);

#ifdef __cplusplus
}
#endif

#endif // _RMAI_INFO_PARSE_API_H_
