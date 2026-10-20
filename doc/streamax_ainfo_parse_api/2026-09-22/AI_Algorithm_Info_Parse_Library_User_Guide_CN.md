# AI 算法信息解析库使用说明

## 文档信息


| 项目      | 内容                                                |
| ------- | ------------------------------------------------- |
| 文档版本    | V1.0.0                                            |
| 库名称     | `rmai_info_parse`（RMAIInfo_Parse_API）             |
| 输出方式    | **C 变长结构体 binary buffer**（`RMAIInfo_ParseStruct`） |
| 协议头文件   | `rmai_info_parse_struct_protocol.h`               |
| API 头文件 | `rmai_info_parse_api.h`                           |
| 参考文档    | 《音视频帧包装格式_标准第二代.doc》                              |
| Demo 工程 | `streamax_video_aiinfo_parse`（§10）                |


> 本文档描述如何调用rmai_info_parse_api接口，从锐明的264/265录像文件中，解析出ADAS/DSC的检测结果，结果以二进制数据的方式输出。数据在内存中的排布，参考本文协议部分的描述。协议中涉及的字段定义以sdk包内 `rmai_info_parse_struct_protocol.h` 为准。

---

## 1. 概述

`rmai_info_parse` 解析视频帧头 AI 叠加信息，输出 ADAS 与 DSC（驾驶员状态监测）检测结果。

调用 `RMAIInfo_ParseStruct()` 得到 binary buffer，调用方按本文第 2~8 章查阅各算法 DataBlock（算法业务数据） 含义与报警/状态枚举。

**典型数据流**：

```
264/265 码流
  → RMSTREAM_HEADER + 扩展信息
  → RMFI2_VIDEOAIINFO（lInfoType = 13）
  → RMAI_Memory
  → RMAIInfo_ParseStruct()
  → 解码 buffer
```

---



## 2. 交付物与 API



### 2.1 交付物概览

交付包含两部分：


| 交付物         | 目录                                    | 用途                                                  |
| ----------- | ------------------------------------- | --------------------------------------------------- |
| **SDK 解析库** | `rmai_info_parse_api/{platform_key}/` | 集成到客户自有程序，调用 `RMAIInfo_ParseStruct` 获取 Struct 二进制结果 |
| **Demo 工程** | `streamax_video_aiinfo_parse/`        | 参考示例：从 Streamax 264/265 录像提取 AI 叠加检测结果，并叠加绘制到视频画面   |


**平台标识** `{platform_key}` **与编译链**：SDK 与 Demo 预编译包按下列平台目录交付；客户须选用与目标运行环境匹配的 `{platform_key}`。


| `{platform_key}`    | 目标平台         | 编译链 / SDK 版本                                                                                       | 运行时库   |
| ------------------- | ------------ | -------------------------------------------------------------------------------------------------- | ------ |
| `x86_64_gcc_13_3_0` | Linux x86_64 | **GCC 13.3.0**；构建基线 **glibc 2.39**（Ubuntu 24.04）；运行时需 **glibc ≥ 2.38**                             | `.so`  |
| `x64_msvc_2026`     | Windows x64  | **Visual Studio 2026**（MSVC 14.50，`MSVC_VERSION ≥ 1950`）；**Windows SDK 10.0.26100.0**；工具集 **v145** | `.dll` |
| `x86_msvc_2026`     | Windows x86  | 同 `x64_msvc_2026`                                                                                  | `.dll` |


---



#### 2.1.1 SDK 包（`rmai_info_parse_api/{platform_key}/`）

按上表 `{platform_key}` 选用对应目录（如 Linux 用 `rmai_info_parse_api/x86_64_gcc_13_3_0/`）。


| 路径                                          | 说明                                   |
| ------------------------------------------- | ------------------------------------ |
| `include/rmai_info_parse_api.h`             | 生命周期 API                             |
| `include/rmai_info_parse_struct_protocol.h` | Struct 协议                            |
| `lib/librmai_info_parse.so`（或 `.dll`）       | 解析库                                  |
| `lib/librmAIResultParse.so`（或 `.dll`）       | 运行时依赖                                |
| `opencv453/`                                | OpenCV 运行时库（**解析库必须依赖**，如提取安全带轮廓时需要） |


客户二次开发时需部署与目标平台匹配的 SDK 包（含 `librmai_info_parse`、`librmAIResultParse` 及 `opencv453`）。

---



#### 2.1.2 Demo 工程（`streamax_video_aiinfo_parse/`）

Demo 与 SDK **共用同一** `{platform_key}` **与编译链**预编译；`release/bin/{linux|x64|x86}/` 下可执行文件及 `rmai_info_parse_api/` 运行库须与上表平台一致。客户自行编译 Demo 时，Linux 需 **GCC 13.3.0**，Windows 需 **VS2026 + Windows SDK 10.0.26100.0**（见 `build.sh` / `build_windows.bat`）。

**用途**：面向客户的**完整参考示例**，演示如何：

1. 读取 **Streamax 标准第二代** 264/265 录像文件；
2. 从帧头扩展区提取 `lInfoType = 13` 的 AI 叠加信息，调用 `RMAIInfo_ParseStruct` 得到检测结果；
3. 按本文第 4~5 章解码 Struct buffer（检测框、报警、状态等）；
4. 将解析结果**按时间戳匹配**到对应视频帧，绘制到画面上（检测框、车道线、报警文字、距离等），并可选输出带叠加的 MP4。

**典型处理流程**：

```
Streamax 264/265 录像
  → 逐帧读取 RMSTREAM 帧头 + 扩展信息
  → 提取 RMFI2_VIDEOAIINFO（type 13）→ 构造 RMAI_Memory
  → RMAIInfo_ParseStruct → 解码 buffer
  → 按 PTS 与视频帧对齐 → 绘制到 OpenCV/输出视频
```

**工程结构（摘要）**：

```
streamax_video_aiinfo_parse/
├── rmai_info_parse_api/     # SDK（与交付包一致）
├── third_party/             # Demo 专用依赖（ffmpeg、cryptopp 等）
├── src/
│   ├── main.cpp             # 程序入口
│   ├── func.cpp             # 264/265 帧头解析、AI body 提取
│   ├── struct_decode.cpp    # Struct buffer 解码、可绘制元素收集
│   ├── overlay_manager.cpp  # 按时间戳管理多组 AI 结果
│   ├── overlay_drawer.cpp   # 检测框/报警/状态叠加绘制
│   └── video_process.cpp    # 录像读取、帧对齐、输出 MP4
├── build.sh / build_windows.bat
└── release/bin/             # 预编译可执行文件与运行库
```

**与客户集成的关系**：Demo 展示了「帧头提取 → API 调用 → Struct 解码 → 画面展示」的端到端链路；客户可参考其中 **帧头解析**（`func.cpp`）与 **Struct 解码**（`struct_decode.cpp`）逻辑，替换为自己的播放器或业务 UI。**OpenCV 为解析库运行时必需**，需随 SDK 一并部署；Demo 内 ffmpeg 视频读写/输出逻辑可按需参考，非集成必需。

编译、运行及批量测试详见 **§10**。

### 2.2 核心类型

```c
#include "rmai_info_parse_api.h"

/* 句柄：每路视频一个实例 */
typedef int RMAI_HANDLE;
#define RMAI_INVALID_HANDLE (-1)

/* 图像尺寸（Create 时传入，用于坐标缩放基准） */
typedef struct {
    int width;          /* 图像宽度（像素），与码流 VIDEOINFO 一致 */
    int height;         /* 图像高度（像素） */
    char reserved[56];  /* 保留，填 0 */
} RMAI_ImageSize;

/* 帧头输入：AI 叠加 body 段（见 §3.3 构造方式） */
typedef struct {
    unsigned long long aiPts;   /* AI 叠加时间戳 PTS，写入输出 timestamp */
    unsigned char*     data;    /* 叠加 body 起始地址（不含 RMFI2_VIDEOAIINFO 头） */
    int                size;    /* body 字节长度，等于 lAIInfoLen */
    char               reserved[48]; /* 保留，填 0 */
} RMAI_Memory;

/* ParseStruct 输出 */
typedef struct {
    const uint8_t* buffer;      /* 协议 binary 起始地址（库内缓存） */
    int            buffer_size; /* buffer 有效字节数 */
} RMAI_StructOutput;
```

**类型字段说明**：


| 类型                  | 字段                 | 说明                                                        |
| ------------------- | ------------------ | --------------------------------------------------------- |
| `RMAI_ImageSize`    | `width` / `height` | 当前视频分辨率，用于检测框坐标缩放；通常取自同帧 `RMFI2_VIDEOINFO`（`lInfoType=1`） |
| `RMAI_ImageSize`    | `reserved[56]`     | 保留，初始化时填 0                                                |
| `RMAI_Memory`       | `aiPts`            | 来自 `RMFI2_VIDEOAIINFO.ullPts`，写入输出各算法组 `timestamp`        |
| `RMAI_Memory`       | `data`             | 指向 AI 叠加 **body** 首地址，**不包含** `RMFI2_VIDEOAIINFO` 结构体头    |
| `RMAI_Memory`       | `size`             | body 长度（字节），等于 `lAIInfoLen`；须 `> 0` 且 `data` 非空           |
| `RMAI_Memory`       | `reserved[48]`     | 保留，填 0                                                    |
| `RMAI_StructOutput` | `buffer`           | 解析结果二进制数据；**不可** `free`，见 §2.5                            |
| `RMAI_StructOutput` | `buffer_size`      | 有效数据长度；空结果时为 20（仅输出头）                                     |




### 2.3 API 详细说明

> **并发约定**：每路视频使用**独立句柄**；**不同句柄**可并发调用各 API。**同一句柄**的 `Destroy` 不得与 `ParseStruct`、`Clear` 等 API 并发。详见 §2.5。



#### 2.3.1 RMAIInfo_Parse_Create — 创建解析实例

**功能**：为一路视频创建解析实例。每路视频（或每路码流）应创建**独立句柄**。

**函数原型**：

```c
RMAI_HANDLE RMAIInfo_Parse_Create(RMAI_ImageSize imageSize);
```

**入参**：


| 参数          | 类型               | 说明                               |
| ----------- | ---------------- | -------------------------------- |
| `imageSize` | `RMAI_ImageSize` | 图像宽高；`width`、`height` 须与当前解析视频一致 |


**返回值**：


| 返回值                       | 含义      | 调用方处理                   |
| ------------------------- | ------- | ----------------------- |
| `>= 0` 的整数                | 有效句柄 ID | 保存句柄，供后续 API 使用         |
| `RMAI_INVALID_HANDLE`（-1） | 创建失败    | 检查参数、内存或实例数上限，不可继续使用该句柄 |


**使用示例**：

```c
RMAI_ImageSize size = {1920, 1080, {0}};
RMAI_HANDLE handle = RMAIInfo_Parse_Create(size);
if (handle == RMAI_INVALID_HANDLE) {
    /* 创建失败，终止或重试 */
}
```

**注意**：每个 `Create` 必须对应一次 `Destroy`（见 §2.3.3）。

---



#### 2.3.2 RMAIInfo_ParseStruct — 解析 AI 叠加并输出 Struct buffer

**功能**：解析一次 `RMAI_Memory`（通常对应一个 `lInfoType=13` 的 AI 内存段），输出 C 变长结构体 binary buffer。调用方按第 4~5 章自行解码 `out->buffer`。

**函数原型**：

```c
int RMAIInfo_ParseStruct(
    RMAI_HANDLE handle,
    RMAI_Memory memory,
    RMAI_StructOutput* out);
```

**入参**：


| 参数             | 类型                   | 说明                                          |
| -------------- | -------------------- | ------------------------------------------- |
| `handle`       | `RMAI_HANDLE`        | `RMAIInfo_Parse_Create` 返回的有效句柄             |
| `memory`       | `RMAI_Memory`        | AI 叠加 body 数据；`data` 非空且 `size > 0`         |
| `memory.aiPts` | `unsigned long long` | 算法 PTS，写入输出 `RMAI_AlgGroupHeader.timestamp` |
| `memory.data`  | `unsigned char*`     | body 首地址（不含 `RMFI2_VIDEOAIINFO` 头）          |
| `memory.size`  | `int`                | body 字节数（`lAIInfoLen`）                      |
| `out`          | `RMAI_StructOutput*` | 输出结构体指针，**不可为 NULL**                        |


**返回值**：


| 返回值  | 含义  | 调用方处理                                                                 |
| ---- | --- | --------------------------------------------------------------------- |
| `0`  | 成功  | 读取 `out->buffer`、`out->buffer_size` 并解码；若 `group_count=0` 表示本段无有效算法输出 |
| `-1` | 失败  | 检查句柄是否有效、`out` 是否为 NULL、`memory.data/size` 是否合法                       |


**输出使用**：


| 输出字段               | 说明                                        |
| ------------------ | ----------------------------------------- |
| `out->buffer`      | 指向库内缓存的协议二进制数据，布局见第 4 章                   |
| `out->buffer_size` | 有效字节数；须 `>= sizeof(RMAI_AlgOutputHeader)` |


**使用示例**：

```c
RMAI_StructOutput out = {0};
if (0 != RMAIInfo_ParseStruct(handle, memory, &out)) {
    /* 解析失败 */
    return;
}

/* 成功：解码 out.buffer（即使 group_count=0 亦为成功） */
if (out.buffer_size >= (int)sizeof(RMAI_AlgOutputHeader)) {
    decode_struct_output(&out);
}
```

**注意**：

- `out->buffer` 在**同句柄**下次调用 `RMAIInfo_ParseStruct` 之前有效；请及时解码或拷贝所需数据。
- **同一句柄**不建议多线程并发调用。
- 一帧内若有多个 `RMFI2_VIDEOAIINFO` 块，须分别构造 `memory` 并多次调用（见 §2.4.4）。

---



#### 2.3.3 RMAIInfo_Parse_Clear — 清空解析状态

**功能**：清除实例内部历史状态（含报警去重等缓存）。**切换视频文件或码流前必须调用**，否则可能因历史状态导致误报。

**函数原型**：

```c
int RMAIInfo_Parse_Clear(RMAI_HANDLE handle);
```

**入参**：


| 参数       | 类型            | 说明       |
| -------- | ------------- | -------- |
| `handle` | `RMAI_HANDLE` | 待清空的实例句柄 |


**返回值**：


| 返回值  | 含义  | 调用方处理         |
| ---- | --- | ------------- |
| `0`  | 成功  | 可继续用同一句柄解析新视频 |
| `-1` | 失败  | 句柄无效或内部错误     |


---



#### 2.3.4 RMAIInfo_Parse_Destroy — 销毁实例

**功能**：销毁由 `Create` 创建的实例，释放库内资源。

**函数原型**：

```c
int RMAIInfo_Parse_Destroy(RMAI_HANDLE handle);
```

**入参**：


| 参数       | 类型            | 说明       |
| -------- | ------------- | -------- |
| `handle` | `RMAI_HANDLE` | 待销毁的实例句柄 |


**返回值**：


| 返回值  | 含义  | 调用方处理      |
| ---- | --- | ---------- |
| `0`  | 成功  | 句柄失效，不可再使用 |
| `-1` | 失败  | 句柄无效       |


**注意**：每个 `Create` 必须对应一次 `Destroy`；销毁后勿再传入该句柄。**同一句柄**的 `Destroy` 须在其他线程完成该句柄上的 `ParseStruct`、`Clear` 等调用后再执行，不可并发。

---



#### 2.3.5 RMAIInfo_Parse_IsValid — 检查句柄有效性

**功能**：判断句柄是否为库管理的有效实例。

**函数原型**：

```c
int RMAIInfo_Parse_IsValid(RMAI_HANDLE handle);
```

**入参**：


| 参数       | 类型            | 说明     |
| -------- | ------------- | ------ |
| `handle` | `RMAI_HANDLE` | 待检查的句柄 |


**返回值**：


| 返回值 | 含义            |
| --- | ------------- |
| `1` | 句柄有效          |
| `0` | 句柄无效（未创建或已销毁） |


---



#### 2.3.6 RMAIInfo_Parse_GetVersion — 获取库版本

**功能**：获取解析库版本字符串，用于集成调试、问题定位与版本核对。

**函数原型**：

```c
const char* RMAIInfo_Parse_GetVersion(void);
```

**入参**：无。

**返回值**：


| 返回值        | 含义                                  | 调用方处理                   |
| ---------- | ----------------------------------- | ----------------------- |
| 非 NULL 字符串 | 版本号，如 `"AIInfo_Parse_Ver_25112112"` | 可直接打印或记录日志；**勿** `free` |


**使用示例**：

```c
const char* ver = RMAIInfo_Parse_GetVersion();
printf("rmai_info_parse version: %s\n", ver ? ver : "unknown");
```

---



#### 2.3.7 API 速查表


| 接口                          | 功能摘要                    | 返回值                        |
| --------------------------- | ----------------------- | -------------------------- |
| `RMAIInfo_Parse_Create`     | 创建实例                    | 句柄 / `RMAI_INVALID_HANDLE` |
| `RMAIInfo_ParseStruct`      | **解析并输出 Struct buffer** | **0 / -1**                 |
| `RMAIInfo_Parse_Clear`      | 切换视频前清空状态               | 0 / -1                     |
| `RMAIInfo_Parse_Destroy`    | 销毁实例                    | 0 / -1                     |
| `RMAIInfo_Parse_IsValid`    | 检查句柄                    | 1 有效 / 0 无效                |
| `RMAIInfo_Parse_GetVersion` | 库版本字符串                  | `const char*`              |




### 2.4 调用流程

**端到端流程**：

```
[初始化]  RMAIInfo_Parse_Create(1920×1080)
              ↓
[逐帧]    解析帧头 → 构造 RMAI_Memory → RMAIInfo_ParseStruct → 解码 out.buffer
              ↓
[换视频]  RMAIInfo_Parse_Clear()
              ↓
[退出]    RMAIInfo_Parse_Destroy()
```



#### 2.4.1 创建与销毁

```c
RMAI_ImageSize imageSize = {1920, 1080, {0}};
RMAI_HANDLE handle = RMAIInfo_Parse_Create(imageSize);
if (handle == RMAI_INVALID_HANDLE) {
    /* 创建失败 */
}

/* ... 逐帧解析 ... */

RMAIInfo_Parse_Destroy(handle);
```



#### 2.4.2 切换视频

切换不同视频文件/码流前，**必须**调用 `RMAIInfo_Parse_Clear(handle)`，否则历史报警状态未清除可能导致误报。

#### 2.4.3 解析单帧

```c
RMAI_StructOutput out = {0};
if (RMAIInfo_ParseStruct(handle, memory, &out) != 0) {
    /* 解析失败 */
    return;
}

/* out.buffer / out.buffer_size 即为当帧结果，按第 4~5 章解码 */
decode_struct_output(&out);
```



#### 2.4.4 一帧多个 AI 信息块

一帧扩展区可能含多个 `RMFI2_VIDEOAIINFO`（`lInfoType = 13`），须**逐个**构造 `RMAI_Memory` 并调用 `RMAIInfo_ParseStruct`：

```c
/* 伪代码：遍历本帧所有 lInfoType=13 的扩展块 */
for (each RMFI2_VIDEOAIINFO* pAiInfo in current_frame) {
    RMAI_Memory memory = {0};
    memory.aiPts = pAiInfo->ullPts;
    memory.data  = (unsigned char*)pAiInfo + sizeof(RMFI2_VIDEOAIINFO);
    memory.size  = (int)pAiInfo->lAIInfoLen;

    RMAI_StructOutput out = {0};
    if (0 == RMAIInfo_ParseStruct(handle, memory, &out)) {
        decode_struct_output(&out);
    }
}
```



### 2.5 内存生命周期与空结果


| 项目                | 说明                                                                          |
| ----------------- | --------------------------------------------------------------------------- |
| `out.buffer` 生命周期 | 指向库内缓存；**同一句柄**下一次 `RMAIInfo_ParseStruct()` 之前有效；**不可** `free`              |
| 空结果               | 当帧无有效 AI 数据时，`group_count = 0`，`total_size = 20`（仅含 `RMAI_AlgOutputHeader`） |
| 句柄隔离              | 每路视频（码流）使用独立 `RMAI_HANDLE`，各实例解析状态与输出缓存互不影响                                 |
| 多路并发              | **不同句柄**可同时调用 `Create` / `ParseStruct` / `Clear` / `Destroy` 等 API（推荐用法）    |
| 同句柄并发             | 库内对同句柄操作串行化；**勿**依赖同句柄多线程并行解析                                               |
| Destroy 约束        | **同一句柄**的 `Destroy` 不得与 `ParseStruct`、`Clear` 等 API 并发；须待该句柄上所有解析完成后再销毁     |


---



## 3. 视频帧头与 RMAI_Memory

本节说明如何从 264/265 码流帧头取得 `RMAIInfo_ParseStruct` 的输入参数（依据《音视频帧包装格式_标准第二代.doc》）。

### 3.1 标准第二代帧结构

每帧数据由 **帧头 + 扩展数据 + 音视频裸流** 组成：

```
[ RMSTREAM_HEADER ][ 扩展信息块 × N ][ 视频/音频裸数据 ]
```

**RMSTREAM_HEADER**（帧头；定义与 Demo `func.h` 一致，依据《音视频帧包装格式_标准第二代.doc》）：

```c
struct RMSTREAM_HEADER
{
    unsigned int  lFrameType;      /*I 帧为x2dc ，p帧为x3dc，音频帧为x4dc，其中x表示通道号，0x00为通道1，0x01为通道2，0x02为通道3，...，0x0f为通道16，0x10为通道17，...，依次类推 */
    unsigned long lFrameLen : 24;  /* 仅视频帧数据长度 */
    unsigned long lStreamExam : 8; /* 码流校验 */
    unsigned long lExtendLen : 24; /* 仅扩展数据长度 */
    unsigned long lExtendCount : 8;
};
```


| 字段             | 位宽     | 说明                                                                                              |
| -------------- | ------ | ----------------------------------------------------------------------------------------------- |
| `lFrameType`   | 32 bit | I 帧为x2dc ，p帧为x3dc，音频帧为x4dc，其中x表示通道号，0x00为通道1，0x01为通道2，0x02为通道3，...，0x0f为通道16，0x10为通道17，...，依次类推 |
| `lFrameLen`    | 24 bit | 仅视频帧数据长度                                                                                        |
| `lStreamExam`  | 8 bit  | 码流校验                                                                                            |
| `lExtendLen`   | 24 bit | 仅扩展数据长度                                                                                         |
| `lExtendCount` | 8 bit  | 扩展信息条目个数                                                                                        |


扩展区由多个 **RMS_INFOTYPE** 条目串联（结构体的基本信息定义，根据第二代标准帧结构文档定义）：

```c
struct RMS_INFOTYPE
{
    unsigned long lInfoType : 8;
    unsigned long lInfoLength : 24;
};
```


| 字段            | 位宽     | 说明           |
| ------------- | ------ | ------------ |
| `lInfoType`   | 8 bit  | 信息类型编号       |
| `lInfoLength` | 24 bit | 本条信息总长度（含本头） |


AI叠加信息为的lInfoType = 13，一个扩展区里，可能有多个lInfoType = 13的AI叠加数据（RMFI2_VIDEOAIINFO）。

### 3.2 AI 叠加信息（lInfoType = 13）

AI 算法结果位于 `RMFI2_VIDEOAIINFO`（`lInfoType = 13`）：

```c
#pragma pack(4)

struct RMS_INFOTYPE {
    unsigned long lInfoType   : 8;
    unsigned long lInfoLength : 24;
};

struct RMFI2_VIDEOAIINFO {
    RMS_INFOTYPE       stuInfoType;   /* lInfoType = 13 */
    unsigned int       lReserve;
    unsigned long long ullPts;        /* AI叠加信息pts(注意-这个和本视频帧的pts不一致) */
    unsigned int       lAIInfoLen;    /* body 长度 */
    char               body[0];       /* 叠加信息内容 */
};

#pragma pack()
```

特别注意：
需要注意的是，用户在视频画面绘制展示AI叠加信息时，需要根据AI叠加信息的时间戳（ullPts）与264/265的视频时间戳，将AI叠加信息与视频帧进行对齐处理，才能正确的展示当前视频帧对应的AI叠加信息。

### 3.3 构造 RMAI_Memory

```c
RMFI2_VIDEOAIINFO* pAiInfo = ...; /* 指向扩展区中的 AI 信息块 */

RMAI_Memory memory = {0};
memory.aiPts = pAiInfo->ullPts;
memory.data  = (unsigned char*)pAiInfo + sizeof(RMFI2_VIDEOAIINFO);
memory.size  = (int)pAiInfo->lAIInfoLen;
```

**注意**：

- `memory.data` 指向 **body**，不包含 `RMFI2_VIDEOAIINFO` 头部。
- 视频宽高可从同帧扩展区 `RMFI2_VIDEOINFO`（`lInfoType = 1`）读取，用于 `RMAIInfo_Parse_Create` 的 `imageSize`。

---



## 4. Struct 协议总览



### 4.1 分层结构

```
buffer
 ├─ RMAI_AlgOutputHeader                 ← 第 1 层：整包输出头
 ├─ RMAI_AlgGroupHeader[group_count]     ← 第 2 层：算法组索引
 └─ Payload × group_count                ← 第 3 层：各算法组业务数据
      ├─ RMAI_AlgGroupPayloadHeader
      ├─ RMAI_AlgGroupDataBlockHeader[data_block_count]
      └─ DataBlock 数据区
```

**地址公式**（解码时必须使用）：

```
DataBlock 数据地址 = buffer + group.payload_offset + block.data_offset
```

`block.data_offset` 相对 **Payload 起点**，不是相对 buffer 起点。

#### 4.1.1 举例：一帧同时含 FCW 车辆 + PCW 行人

假设本帧解析出 **2 个算法组**：FCW 检测到 2 辆车（其中 1 辆为目标的 distance=25.5m），PCW 检测到 1 个行人。`RMAIInfo_ParseStruct()` 输出的 buffer 可理解为：

```
buffer
│
├─ [第 1 层] RMAI_AlgOutputHeader                         offset = 0
│     magic = 'RMAI'，version = 1
│     group_count = 2
│     groups_offset = 20
│     total_size = 252        ← 示意值
│
├─ [第 2 层] RMAI_AlgGroupHeader[0]                       ← FCW/HMW
│     alg_type = 2，group_id = 2，category = ADAS(1)
│     timestamp = memory.aiPts
│     payload_offset = 76
│     payload_size   = 128
│
├─ [第 2 层] RMAI_AlgGroupHeader[1]                       ← PCW
│     alg_type = 4，group_id = 4，category = ADAS(1)
│     payload_offset = 204
│     payload_size   = 48
│
├─ [第 3 层] Payload #0（FCW，payload_offset=76）
│   ├─ RMAI_AlgGroupPayloadHeader（8B）
│   │     data_block_count = 2，alg_type = 2
│   ├─ RMAI_AlgGroupDataBlockHeader[0]（20B）
│   │     block_type = RMAI_ADAS_VEHICLES (1)
│   │     elem_size = 16，count = 2，data_offset = 48
│   ├─ RMAI_AlgGroupDataBlockHeader[1]（20B）
│   │     block_type = RMAI_ALG_ALARMS (3)
│   │     elem_size = 8，count = 1，data_offset = 80
│   └─ DataBlock 数据区
│         ├─ vehicles[0..1]  → RMAI_Vehicle × 2（各 16B，见 §5.5）
│         │     例：x=320,y=180,w=120,h=90, flags=0x01, distance=25.5, is_target=1
│         └─ alarms[0]       → RMAI_AlarmItem（8B）
│               alarm_type = 101（RMAI_ALARM_ADAS_FCW_COLLISION）
│
└─ [第 3 层] Payload #1（PCW，payload_offset=204）
    ├─ RMAI_AlgGroupPayloadHeader
    │     data_block_count = 2，alg_type = 4
    ├─ RMAI_AlgGroupDataBlockHeader[0]
    │     block_type = RMAI_ADAS_PEDESTRIANS (7)
    │     elem_size = 16，count = 1，data_offset = 48
    ├─ RMAI_AlgGroupDataBlockHeader[1]
    │     block_type = RMAI_ALG_ALARMS (3)
    │     elem_size = 8，count = 1，data_offset = 64
    └─ DataBlock 数据区
          ├─ pedestrians[0]  → RMAI_Pedestrian × 1（16B，见 §5.5）
          │     例：x=500,y=200,w=40,h=120, flags=0x01, distance=8.2, is_target=1
          └─ alarms[0]       → RMAI_AlarmItem（8B）
                alarm_type = 105（RMAI_ALARM_ADAS_PCW_PEDESTRIAN_COLLISION）
```

**对应关系速查**：


| 层级    | 结构                                     | 含义                      |
| ----- | -------------------------------------- | ----------------------- |
| 第 1 层 | `RMAI_AlgOutputHeader`                 | 整包目录：算法组个数、各组索引表偏移      |
| 第 2 层 | `RMAI_AlgGroupHeader[i]`               | 算法组书签：类型、时间戳、Payload 位置 |
| 第 3 层 | PayloadHeader + DataBlockHeader[] + 数据 | 该组具体输出：检测框、报警、状态等       |


> **说明**：上例中 `payload_offset`、`data_offset` 等偏移仅为示意；实际值由库动态计算。**解码时始终读取 header 中的偏移字段**，不要写死数值。

**解码路径示例**（读取 FCW 目标车辆及距离）：

```c
const RMAI_AlgOutputHeader* hdr = (const RMAI_AlgOutputHeader*)out->buffer;
const RMAI_AlgGroupHeader* groups =
    (const RMAI_AlgGroupHeader*)(out->buffer + hdr->groups_offset);

/* 按 alg_type 查找 FCW 组 */
const RMAI_AlgGroupHeader* fcw = NULL;
for (uint16_t i = 0; i < hdr->group_count; ++i) {
    if (groups[i].alg_type == RMAI_ALG_FCW_HMW) {
        fcw = &groups[i];
        break;
    }
}
if (NULL == fcw) return;

const uint8_t* payload = out->buffer + fcw->payload_offset;
const RMAI_AlgGroupPayloadHeader* ph =
    (const RMAI_AlgGroupPayloadHeader*)payload;
const RMAI_AlgGroupDataBlockHeader* blocks =
    (const RMAI_AlgGroupDataBlockHeader*)(payload + sizeof(RMAI_AlgGroupPayloadHeader));

for (int i = 0; i < ph->data_block_count; ++i) {
    if (blocks[i].block_type != RMAI_ADAS_VEHICLES) continue;

    const RMAI_Vehicle* vehicles =
        (const RMAI_Vehicle*)(payload + blocks[i].data_offset);
    for (uint32_t k = 0; k < blocks[i].count; ++k) {
        if (!vehicles[k].is_target) continue;

        float dist_m = 0.0f;
        if (vehicles[k].flags & 0x01) {
            dist_m = vehicles[k].distance;  /* 单位：米 */
        }
        /* 使用 vehicles[k].x/y/width/height, dist_m */
    }
    break;
}
```



### 4.2 协议常量


| 宏                               | 值            | 说明                          |
| ------------------------------- | ------------ | --------------------------- |
| `RMAI_STRUCT_MAGIC`             | `0x524D4149` | 'RMAI'                      |
| `RMAI_STRUCT_VERSION`           | `0x0001`     | v1                          |
| `RMAI_STRUCT_TEXT_MAX_LEN`      | 64           | `RMAI_StatusItem.text` 最大长度 |
| `RMAI_STRUCT_CONTOUR_NO_PARENT` | -1           | 轮廓无父 ID                     |




### 4.3 布局头 sizeof（`#pragma pack(4)`，实测）


| 结构体                            | sizeof（字节） |
| ------------------------------ | ---------- |
| `RMAI_AlgOutputHeader`         | 20         |
| `RMAI_AlgGroupHeader`          | 28         |
| `RMAI_AlgGroupPayloadHeader`   | 8          |
| `RMAI_AlgGroupDataBlockHeader` | 20         |
| `RMAI_Point`                   | 4          |
| `RMAI_Rect`                    | 8          |
| `RMAI_Vehicle`                 | 16         |
| `RMAI_Pedestrian`              | 16         |
| `RMAI_AlarmItem`               | 8          |
| `RMAI_StatusItem`              | 68         |
| `RMAI_HeadPose`                | 16         |
| `RMAI_VechileSpeed`            | 8          |
| `RMAI_ContourHeader`           | 12         |




### 4.4 算法类型（`alg_type` = `group_id`）

`alg_type` 表示**算法大类（ADAS/DSC）下的子算法类型**（与帧头叠加信息中的 `group_id` 同值）。设备端各子算法**并行运行**，各自负责一项检测功能（如 FCW 前向碰撞、LDW 车道偏离、PCW 行人碰撞等）；多个子算法共同构成 ADAS 或 DSC 大类的完整能力。

**与解析接口的关系**：

- 每次调用 `RMAIInfo_ParseStruct()` 通常只传入**一个** `lInfoType = 13` 的 AI 内存段（`RMAI_Memory`）。
- 该内存段在多数情况下只携带**一个** `alg_type` 的检测结果（报警、检测框、状态等）。
- 输出 buffer 中对应算法组的 `RMAI_AlgGroupHeader.timestamp` 即为该子算法结果的时间戳（来源于 `memory.aiPts`）。

**集成展示建议**：若需呈现完整的 ADAS 画面（同时含 FCW、LDW、PCW 等），应分别解析各子算法输出的 buffer，按**时间戳相近**的原则将多条结果关联后一并展示。DSC 同理（如人脸检测、疲劳、打电话等子算法需分别获取再合并）。每个子算法在设备端跑的帧率不一样，不一定是每个视频帧都能匹配到对应的子算法(如DSC视频帧率为16FPS，但其中一个子算法只跑了5FPS)，这种情况需要客户自己做平滑处理，将一帧算法结果展示到邻近的视频画面上,或者该视频帧直接不展示算法检测结果。

> 说明：少数情况下，单个 `lInfoType = 13` 内存段内也可能包含多个算法组（输出 `group_count > 1`，见 §4.1.1 举例）；此时每个 `RMAI_AlgGroupHeader` 仍对应一个独立的 `alg_type` 与时间戳。


| 值   | 枚举                         | 名称                | category      |
| --- | -------------------------- | ----------------- | ------------- |
| 2   | `RMAI_ALG_FCW_HMW`         | fcw_hmw           | ADAS (1)      |
| 3   | `RMAI_ALG_LDW`             | ldw               | ADAS (1)      |
| 4   | `RMAI_ALG_PCW`             | pcw               | ADAS (1)      |
| 26  | `RMAI_ALG_TSR`             | tsr               | ADAS (1)      |
| 6   | `RMAI_ALG_DSC_FACE_DETECT` | dsc_face_detect   | DSC (2)       |
| 7   | `RMAI_ALG_DSC_FACE_ALIGN`  | dsc_face_align    | **预留，v1 不输出** |
| 8   | `RMAI_ALG_DSC_YAWN_POSE`   | dsc_yawn_and_pose | DSC (2)       |
| 9   | `RMAI_ALG_DSC_EYE_STATUS`  | dsc_eys_status    | DSC (2)       |
| 10  | `RMAI_ALG_DSC_CALL`        | dsc_call          | DSC (2)       |
| 22  | `RMAI_ALG_DSC_BELT`        | dsc_belt          | DSC (2)       |




### 4.5 DataBlock 类型与 elem_size

**通用（ADAS / DSC 共用）**：


| block_type | 枚举                | 元素类型             | elem_size |
| ---------- | ----------------- | ---------------- | --------- |
| 3          | `RMAI_ALG_ALARMS` | `RMAI_AlarmItem` | 8         |


**ADAS（1~9，不含 3）**：


| block_type | 枚举                                  | 元素类型              | elem_size |
| ---------- | ----------------------------------- | ----------------- | --------- |
| 1          | `RMAI_ADAS_VEHICLES`                | `RMAI_Vehicle`    | 16        |
| 2          | `RMAI_ADAS_DETECTION_AREA`          | `RMAI_Rect`       | 8         |
| 4          | `RMAI_ADAS_STATUS`                  | `RMAI_StatusItem` | 68        |
| 5          | `RMAI_ADAS_LEFT_LANE_POINTS`        | `RMAI_Point`      | 4         |
| 6          | `RMAI_ADAS_RIGHT_LANE_POINTS`       | `RMAI_Point`      | 4         |
| 7          | `RMAI_ADAS_PEDESTRIANS`             | `RMAI_Pedestrian` | 16        |
| 8          | `RMAI_ADAS_ROLLING_STOP_COORDS`     | `RMAI_Rect`       | 8         |
| 9          | `RMAI_ADAS_SPEED_LIMIT_SIGN_COORDS` | `RMAI_Rect`       | 8         |


**DSC（100~）**：


| block_type | 枚举                           | 元素类型                | elem_size |
| ---------- | ---------------------------- | ------------------- | --------- |
| 100        | `RMAI_DSC_HEAD_POSE`         | `RMAI_HeadPose`     | 16        |
| 101        | `RMAI_DSC_FACE_RECTS`        | `RMAI_Rect`         | 8         |
| 102        | `RMAI_DSC_FACE_KEY_POINTS`   | `RMAI_Point`        | 4         |
| 103        | `RMAI_DSC_PHONE_RECTS`       | `RMAI_Rect`         | 8         |
| 104        | `RMAI_DSC_SEATBELT_CONTOURS` | 变长轮廓                | 0         |
| 105        | `RMAI_DSC_VECHILE_SPEED`     | `RMAI_VechileSpeed` | 8         |
| 106        | `RMAI_DSC_DETECTION_AREA`    | `RMAI_Rect`         | 8         |


DataBlock 缺失或 `count == 0` 表示无该数据。须按 `block_type` 查找，**不可假设顺序**。

---



## 5. 公共元素结构体字段



### 5.1 RMAI_AlarmItem（所有报警 DataBlock 元素）


| 字段            | 类型         | 取值说明                      |
| ------------- | ---------- | ------------------------- |
| `alarm_type`  | `uint32_t` | `RMAI_AlarmType` 枚举值，见 §7 |
| `reserved[4]` | `uint8_t`  | 编码时填 0                    |




### 5.2 RMAI_StatusItem（`RMAI_ADAS_STATUS` 元素）


| 字段            | 类型         | 取值说明                               |
| ------------- | ---------- | ---------------------------------- |
| `status_type` | `uint32_t` | `RMAI_StatusType` 枚举值，见 §8         |
| `text`        | `char[64]` | 状态文本字符串，最大 63 字符（含 `\0` 预留），示例见 §8 |




### 5.3 RMAI_Point


| 字段  | 类型         | 说明   |
| --- | ---------- | ---- |
| `x` | `uint16_t` | 像素 X |
| `y` | `uint16_t` | 像素 Y |




### 5.4 RMAI_Rect（检测框）


| 字段                          | 类型         | 说明                |
| --------------------------- | ---------- | ----------------- |
| `x`, `y`, `width`, `height` | `uint16_t` | 检测框左上角坐标 + 宽高（像素） |




### 5.5 RMAI_Vehicle / RMAI_Pedestrian（含 distance）

二者内存布局相同（`sizeof = 16`，`#pragma pack(4)`）：


| 偏移  | 字段            | 类型         | 说明                             |
| --- | ------------- | ---------- | ------------------------------ |
| 0   | `x`           | `uint16_t` | 检测框左上角 X（像素）                   |
| 2   | `y`           | `uint16_t` | 检测框左上角 Y（像素）                   |
| 4   | `width`       | `uint16_t` | 检测框宽度（像素）                      |
| 6   | `height`      | `uint16_t` | 检测框高度（像素）                      |
| 8   | `distance`    | `float`    | 距离（**米**）；仅 `flags bit0=1` 时有效 |
| 12  | `is_target`   | `uint8_t`  | 是否目标：`1`=是，`0`=否               |
| 13  | `flags`       | `uint8_t`  | bit0=1 表示 `distance` 有效        |
| 14  | `reserved[2]` | `uint8_t`  | 保留，填 0                         |


**读取 distance 的规则**：

```c
float dist_m = 0.0f;
if (vehicle.flags & 0x01) {
    dist_m = vehicle.distance;   /* 有效距离，单位米 */
} else {
    /* 本帧该目标无距离数据，勿读取 distance 字段 */
}
```

`RMAI_Pedestrian` 的 `distance` / `flags` 规则与上表完全相同。

### 5.6 布局头字段速查


| 结构体                            | 关键字段                                                                              | 说明                              |
| ------------------------------ | --------------------------------------------------------------------------------- | ------------------------------- |
| `RMAI_AlgOutputHeader`         | `magic`, `version`, `group_count`, `total_size`, `groups_offset`                  | buffer 入口；校验 magic=`0x524D4149` |
| `RMAI_AlgGroupHeader`          | `timestamp`, `group_id`, `alg_type`, `category`, `payload_offset`, `payload_size` | 定位各算法组 Payload                  |
| `RMAI_AlgGroupPayloadHeader`   | `alg_type`, `data_block_count`                                                    | Payload 内 DataBlock 个数          |
| `RMAI_AlgGroupDataBlockHeader` | `block_type`, `elem_size`, `count`, `data_offset`, `data_size`                    | 定位具体 DataBlock 数据               |




### 5.7 坐标系

- 原点：图像左上角 `(0,0)`；X 向右；Y 向下；单位像素。

---



## 6. 各算法输出明细

以下按算法组列出 **Struct 输出**可能包含的 DataBlock 类型、元素结构及报警/状态枚举。**DataBlock 缺失或** `count == 0` **表示该数据为空**。字段定义以 `rmai_info_parse_struct_protocol.h` 为准；公共元素结构见 §5。

---



### 6.1 FCW/HMW（`alg_type = 2`，`RMAI_ALG_FCW_HMW`）



#### 6.1.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型               | 元素类型              | elem_size | 含义            |
| ---------- | -------------------------- | ----------------- | --------- | ------------- |
| 1          | `RMAI_ADAS_VEHICLES`       | `RMAI_Vehicle`    | 16        | 检测到的车辆列表      |
| 2          | `RMAI_ADAS_DETECTION_AREA` | `RMAI_Rect`       | 8         | FCW 检测区域      |
| 3          | `RMAI_ALG_ALARMS`          | `RMAI_AlarmItem`  | 8         | 报警列表          |
| 4          | `RMAI_ADAS_STATUS`         | `RMAI_StatusItem` | 68        | 状态列表（TTC、车速等） |




#### 6.1.2 RMAI_Vehicle


| 字段                          | 类型         | 说明                               |
| --------------------------- | ---------- | -------------------------------- |
| `x`, `y`, `width`, `height` | `uint16_t` | 车辆检测框（像素）                        |
| `distance`                  | `float`    | 前车距离（**米**）；`flags & 0x01` 为真时有效 |
| `is_target`                 | `uint8_t`  | 是否为目标车辆：`1`=是，`0`=否              |
| `flags`                     | `uint8_t`  | bit0=1 表示 `distance` 有效          |
| `reserved[2]`               | `uint8_t`  | 保留                               |




#### 6.1.3 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.1.4 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.1.6 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.1.5 RMAI_StatusItem


| 字段            | 类型         | 说明                       |
| ------------- | ---------- | ------------------------ |
| `status_type` | `uint32_t` | 状态类型，见 §6.1.7            |
| `text`        | `char[64]` | 状态文本；最大 63 字符（含 `\0` 预留） |




#### 6.1.6 报警


| alarm_type | 枚举名                                      | 说明          |
| ---------- | ---------------------------------------- | ----------- |
| 101        | `RMAI_ALARM_ADAS_FCW_COLLISION`          | 前向碰撞预警（FCW） |
| 102        | `RMAI_ALARM_ADAS_HMW_CAR_DISTANCE_CLOSE` | 车距过近预警（HMW） |




#### 6.1.7 状态


| status_type | 枚举名                                   | 说明                            |
| ----------- | ------------------------------------- | ----------------------------- |
| 1           | `RMAI_STATUS_ADAS_FCW_TTC`            | 前向碰撞时间 TTC；`text` 示例 `"5.0s"` |
| 2           | `RMAI_STATUS_ADAS_HMW_TTC`            | 车距监测时间 TTC；`text` 示例 `"5.0s"` |
| 3           | `RMAI_STATUS_ADAS_CAR_SPEED`          | 当前车速；`text` 示例 `"56km/h"`     |
| 4           | `RMAI_STATUS_ADAS_CAR_RELATIVE_SPEED` | 前车相对速度；`text` 示例 `"4.5km/h"`  |


---



### 6.2 LDW（`alg_type = 3`，`RMAI_ALG_LDW`）



#### 6.2.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型                  | 元素类型              | elem_size | 含义            |
| ---------- | ----------------------------- | ----------------- | --------- | ------------- |
| 3          | `RMAI_ALG_ALARMS`             | `RMAI_AlarmItem`  | 8         | 报警列表          |
| 4          | `RMAI_ADAS_STATUS`            | `RMAI_StatusItem` | 68        | 状态列表（TTC、车速等） |
| 5          | `RMAI_ADAS_LEFT_LANE_POINTS`  | `RMAI_Point`      | 4         | 左侧车道线点集       |
| 6          | `RMAI_ADAS_RIGHT_LANE_POINTS` | `RMAI_Point`      | 4         | 右侧车道线点集       |




#### 6.2.2 RMAI_Point


| 字段  | 类型         | 说明      |
| --- | ---------- | ------- |
| `x` | `uint16_t` | 像素 X 坐标 |
| `y` | `uint16_t` | 像素 Y 坐标 |




#### 6.2.3 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.2.5 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.2.4 RMAI_StatusItem


| 字段            | 类型         | 说明                       |
| ------------- | ---------- | ------------------------ |
| `status_type` | `uint32_t` | 状态类型，见 §6.2.6            |
| `text`        | `char[64]` | 状态文本；最大 63 字符（含 `\0` 预留） |




#### 6.2.5 报警


| alarm_type | 枚举名                                        | 说明     |
| ---------- | ------------------------------------------ | ------ |
| 103        | `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_LEFT`  | 左侧车道偏离 |
| 104        | `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_RIGHT` | 右侧车道偏离 |




#### 6.2.6 状态


| status_type | 枚举名                                   | 说明                            |
| ----------- | ------------------------------------- | ----------------------------- |
| 1           | `RMAI_STATUS_ADAS_FCW_TTC`            | 前向碰撞时间 TTC；`text` 示例 `"5.0s"` |
| 2           | `RMAI_STATUS_ADAS_HMW_TTC`            | 车距监测时间 TTC；`text` 示例 `"5.0s"` |
| 3           | `RMAI_STATUS_ADAS_CAR_SPEED`          | 当前车速；`text` 示例 `"56km/h"`     |
| 4           | `RMAI_STATUS_ADAS_CAR_RELATIVE_SPEED` | 前车相对速度；`text` 示例 `"4.5km/h"`  |


---



### 6.3 PCW（`alg_type = 4`，`RMAI_ALG_PCW`）



#### 6.3.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）。**本组不输出** `RMAI_ADAS_STATUS`**（block_type=4）**。


| block_type | DataBlock 类型            | 元素类型              | elem_size | 含义   |
| ---------- | ----------------------- | ----------------- | --------- | ---- |
| 3          | `RMAI_ALG_ALARMS`       | `RMAI_AlarmItem`  | 8         | 报警列表 |
| 7          | `RMAI_ADAS_PEDESTRIANS` | `RMAI_Pedestrian` | 16        | 行人列表 |




#### 6.3.2 RMAI_Pedestrian


| 字段                          | 类型         | 说明                               |
| --------------------------- | ---------- | -------------------------------- |
| `x`, `y`, `width`, `height` | `uint16_t` | 行人检测框（像素）                        |
| `distance`                  | `float`    | 行人距离（**米**）；`flags & 0x01` 为真时有效 |
| `is_target`                 | `uint8_t`  | 是否为目标行人：`1`=是，`0`=否              |
| `flags`                     | `uint8_t`  | bit0=1 表示 `distance` 有效          |
| `reserved[2]`               | `uint8_t`  | 保留                               |




#### 6.3.3 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.3.4 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.3.4 报警


| alarm_type | 枚举名                                        | 说明     |
| ---------- | ------------------------------------------ | ------ |
| 105        | `RMAI_ALARM_ADAS_PCW_PEDESTRIAN_COLLISION` | 行人碰撞预警 |


---



### 6.4 TSR（`alg_type = 26`，`RMAI_ALG_TSR`）



#### 6.4.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）。**本组不输出** `RMAI_ADAS_STATUS`**（block_type=4）**。


| block_type | DataBlock 类型                        | 元素类型             | elem_size | 含义      |
| ---------- | ----------------------------------- | ---------------- | --------- | ------- |
| 3          | `RMAI_ALG_ALARMS`                   | `RMAI_AlarmItem` | 8         | 报警列表    |
| 8          | `RMAI_ADAS_ROLLING_STOP_COORDS`     | `RMAI_Rect`      | 8         | 停车标志检测框 |
| 9          | `RMAI_ADAS_SPEED_LIMIT_SIGN_COORDS` | `RMAI_Rect`      | 8         | 限速标志检测框 |




#### 6.4.2 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.4.3 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.4.4 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.4.4 报警


| alarm_type | 枚举名                                | 说明     |
| ---------- | ---------------------------------- | ------ |
| 106        | `RMAI_ALARM_TSR_STOP_ALARM`        | 停车标志报警 |
| 107        | `RMAI_ALARM_TSR_SPEED_LIMIT_ALARM` | 限速标志报警 |


---



### 6.5 dsc_face_detect（`alg_type = 6`，`RMAI_ALG_DSC_FACE_DETECT`）



#### 6.5.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型              | 元素类型                | elem_size | 含义                  |
| ---------- | ------------------------- | ------------------- | --------- | ------------------- |
| 3          | `RMAI_ALG_ALARMS`         | `RMAI_AlarmItem`    | 8         | 报警列表                |
| 105        | `RMAI_DSC_VECHILE_SPEED`  | `RMAI_VechileSpeed` | 8         | 车速（`count=1` 时单条记录） |
| 106        | `RMAI_DSC_DETECTION_AREA` | `RMAI_Rect`         | 8         | 驾驶员检测区域             |




#### 6.5.2 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.5.3 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.5.5 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.5.4 RMAI_VechileSpeed


| 字段            | 类型        | 说明             |
| ------------- | --------- | -------------- |
| `speed`       | `int32_t` | 车速，单位 **km/h** |
| `reserved[4]` | `uint8_t` | 保留             |




#### 6.5.5 报警


| alarm_type | 枚举名                    | 说明      |
| ---------- | ---------------------- | ------- |
| 201        | `RMAI_ALARM_NO_DRIVER` | 未检测到驾驶员 |


---



### 6.6 dsc_yawn_and_pose（`alg_type = 8`，`RMAI_ALG_DSC_YAWN_POSE`）



#### 6.6.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型               | 元素类型             | elem_size | 含义                    |
| ---------- | -------------------------- | ---------------- | --------- | --------------------- |
| 3          | `RMAI_ALG_ALARMS`          | `RMAI_AlarmItem` | 8         | 报警列表                  |
| 100        | `RMAI_DSC_HEAD_POSE`       | `RMAI_HeadPose`  | 16        | 头部姿态（`count=1` 时单条记录） |
| 101        | `RMAI_DSC_FACE_RECTS`      | `RMAI_Rect`      | 8         | 人脸检测框                 |
| 102        | `RMAI_DSC_FACE_KEY_POINTS` | `RMAI_Point`     | 4         | 人脸关键点                 |




#### 6.6.2 RMAI_HeadPose


| 字段                     | 类型        | 说明              |
| ---------------------- | --------- | --------------- |
| `yaw`, `pitch`, `roll` | `float`   | 头部姿态角，单位 **弧度** |
| `reserved[4]`          | `uint8_t` | 保留              |




#### 6.6.3 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.6.4 RMAI_Point


| 字段  | 类型         | 说明      |
| --- | ---------- | ------- |
| `x` | `uint16_t` | 像素 X 坐标 |
| `y` | `uint16_t` | 像素 Y 坐标 |




#### 6.6.5 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.6.6 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.6.6 报警


| alarm_type | 枚举名                      | 说明     |
| ---------- | ------------------------ | ------ |
| 202        | `RMAI_ALARM_FATIGUE`     | 驾驶员疲劳  |
| 203        | `RMAI_ALARM_DISTRACTED`  | 驾驶员分心  |
| 206        | `RMAI_ALARM_BLOCK_GLASS` | 红外阻断眼镜 |


---



### 6.7 dsc_eys_status（`alg_type = 9`，`RMAI_ALG_DSC_EYE_STATUS`）



#### 6.7.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型               | 元素类型             | elem_size | 含义       |
| ---------- | -------------------------- | ---------------- | --------- | -------- |
| 3          | `RMAI_ALG_ALARMS`          | `RMAI_AlarmItem` | 8         | 报警列表     |
| 101        | `RMAI_DSC_FACE_RECTS`      | `RMAI_Rect`      | 8         | 人脸检测框    |
| 102        | `RMAI_DSC_FACE_KEY_POINTS` | `RMAI_Point`     | 4         | 人脸/眼部关键点 |




#### 6.7.2 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.7.3 RMAI_Point


| 字段  | 类型         | 说明      |
| --- | ---------- | ------- |
| `x` | `uint16_t` | 像素 X 坐标 |
| `y` | `uint16_t` | 像素 Y 坐标 |




#### 6.7.4 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.7.5 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.7.5 报警


| alarm_type | 枚举名                     | 说明    |
| ---------- | ----------------------- | ----- |
| 202        | `RMAI_ALARM_FATIGUE`    | 驾驶员疲劳 |
| 203        | `RMAI_ALARM_DISTRACTED` | 驾驶员分心 |


---



### 6.8 dsc_call（`alg_type = 10`，`RMAI_ALG_DSC_CALL`）



#### 6.8.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型               | 元素类型             | elem_size | 含义    |
| ---------- | -------------------------- | ---------------- | --------- | ----- |
| 3          | `RMAI_ALG_ALARMS`          | `RMAI_AlarmItem` | 8         | 报警列表  |
| 101        | `RMAI_DSC_FACE_RECTS`      | `RMAI_Rect`      | 8         | 人脸检测框 |
| 102        | `RMAI_DSC_FACE_KEY_POINTS` | `RMAI_Point`     | 4         | 人脸关键点 |
| 103        | `RMAI_DSC_PHONE_RECTS`     | `RMAI_Rect`      | 8         | 手机检测框 |




#### 6.8.2 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.8.3 RMAI_Point


| 字段  | 类型         | 说明      |
| --- | ---------- | ------- |
| `x` | `uint16_t` | 像素 X 坐标 |
| `y` | `uint16_t` | 像素 Y 坐标 |




#### 6.8.4 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.8.5 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.8.5 报警


| alarm_type | 枚举名                         | 说明    |
| ---------- | --------------------------- | ----- |
| 204        | `RMAI_ALARM_PHONE_DETECTED` | 检测到手机 |


---



### 6.9 dsc_belt（`alg_type = 22`，`RMAI_ALG_DSC_BELT`）



#### 6.9.1 DataBlock

本组可能输出的 DataBlock 如下（按 `block_type` 排序；实际 buffer 中顺序不固定，须按 `block_type` 查找）：


| block_type | DataBlock 类型                 | 元素类型             | elem_size | 含义                   |
| ---------- | ---------------------------- | ---------------- | --------- | -------------------- |
| 3          | `RMAI_ALG_ALARMS`            | `RMAI_AlarmItem` | 8         | 报警列表                 |
| 101        | `RMAI_DSC_FACE_RECTS`        | `RMAI_Rect`      | 8         | 人脸检测框                |
| 102        | `RMAI_DSC_FACE_KEY_POINTS`   | `RMAI_Point`     | 4         | 人脸关键点                |
| 104        | `RMAI_DSC_SEATBELT_CONTOURS` | 变长轮廓             | 0         | 安全带轮廓（`count` 为轮廓条数） |




#### 6.9.2 RMAI_Rect


| 字段                | 类型         | 说明           |
| ----------------- | ---------- | ------------ |
| `x`, `y`          | `uint16_t` | 检测框左上角坐标（像素） |
| `width`, `height` | `uint16_t` | 检测框宽高（像素）    |




#### 6.9.3 RMAI_Point


| 字段  | 类型         | 说明      |
| --- | ---------- | ------- |
| `x` | `uint16_t` | 像素 X 坐标 |
| `y` | `uint16_t` | 像素 Y 坐标 |




#### 6.9.4 安全带轮廓（`elem_size = 0`）

每条轮廓布局：

```
[ RMAI_ContourHeader (12B) ][ RMAI_Point × point_count ]
```

**RMAI_ContourHeader**：


| 字段                  | 类型         | 说明                                             |
| ------------------- | ---------- | ---------------------------------------------- |
| `id`                | `int32_t`  | 轮廓 ID                                          |
| `parent_contour_id` | `int32_t`  | 父轮廓 ID；无父为 `RMAI_STRUCT_CONTOUR_NO_PARENT`（-1） |
| `point_count`       | `uint32_t` | 轮廓点数                                           |




#### 6.9.5 RMAI_AlarmItem


| 字段            | 类型         | 说明            |
| ------------- | ---------- | ------------- |
| `alarm_type`  | `uint32_t` | 报警类型，见 §6.9.6 |
| `reserved[4]` | `uint8_t`  | 保留，编码时为 **0** |




#### 6.9.6 报警


| alarm_type | 枚举名                  | 说明    |
| ---------- | -------------------- | ----- |
| 205        | `RMAI_ALARM_NO_BELT` | 未系安全带 |


---



## 7. 报警类型逐条说明（RMAI_AlarmType）

所有报警均位于 `RMAI_ALG_ALARMS`**（block_type=3）**，元素 `RMAI_AlarmItem`**（8 字节）**：


| 字段            | 类型         | 取值                   |
| ------------- | ---------- | -------------------- |
| `alarm_type`  | `uint32_t` | 下表各条 `alarm_type` 数值 |
| `reserved[4]` | `uint8_t`  | **0**                |


buffer 中仅出现下表所列 `alarm_type`；`RMAI_ALARM_NONE`（0）不写入 buffer。

---



### 7.1 ADAS 报警（101–107）



#### 7.1.1 alarm_type = 101 — `RMAI_ALARM_ADAS_FCW_COLLISION`


| 项目                           | 取值                    |
| ---------------------------- | --------------------- |
| 中文说明                         | 前向碰撞预警（FCW）           |
| 输出 `alg_type`                | 2（`RMAI_ALG_FCW_HMW`） |
| `RMAI_AlarmItem.alarm_type`  | **101**               |
| `RMAI_AlarmItem.reserved[4]` | **0**                 |




#### 7.1.2 alarm_type = 102 — `RMAI_ALARM_ADAS_HMW_CAR_DISTANCE_CLOSE`


| 项目                           | 取值          |
| ---------------------------- | ----------- |
| 中文说明                         | 车距过近预警（HMW） |
| 输出 `alg_type`                | 2           |
| `RMAI_AlarmItem.alarm_type`  | **102**     |
| `RMAI_AlarmItem.reserved[4]` | **0**       |




#### 7.1.3 alarm_type = 103 — `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_LEFT`


| 项目                           | 取值                |
| ---------------------------- | ----------------- |
| 中文说明                         | 左侧车道偏离            |
| 输出 `alg_type`                | 3（`RMAI_ALG_LDW`） |
| `RMAI_AlarmItem.alarm_type`  | **103**           |
| `RMAI_AlarmItem.reserved[4]` | **0**             |




#### 7.1.4 alarm_type = 104 — `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_RIGHT`


| 项目                           | 取值      |
| ---------------------------- | ------- |
| 中文说明                         | 右侧车道偏离  |
| 输出 `alg_type`                | 3       |
| `RMAI_AlarmItem.alarm_type`  | **104** |
| `RMAI_AlarmItem.reserved[4]` | **0**   |




#### 7.1.5 alarm_type = 105 — `RMAI_ALARM_ADAS_PCW_PEDESTRIAN_COLLISION`


| 项目                           | 取值                |
| ---------------------------- | ----------------- |
| 中文说明                         | 行人碰撞预警            |
| 输出 `alg_type`                | 4（`RMAI_ALG_PCW`） |
| `RMAI_AlarmItem.alarm_type`  | **105**           |
| `RMAI_AlarmItem.reserved[4]` | **0**             |




#### 7.1.6 alarm_type = 106 — `RMAI_ALARM_TSR_STOP_ALARM`


| 项目                           | 取值                 |
| ---------------------------- | ------------------ |
| 中文说明                         | 停车标志报警             |
| 输出 `alg_type`                | 26（`RMAI_ALG_TSR`） |
| `RMAI_AlarmItem.alarm_type`  | **106**            |
| `RMAI_AlarmItem.reserved[4]` | **0**              |




#### 7.1.7 alarm_type = 107 — `RMAI_ALARM_TSR_SPEED_LIMIT_ALARM`


| 项目                           | 取值      |
| ---------------------------- | ------- |
| 中文说明                         | 限速标志报警  |
| 输出 `alg_type`                | 26      |
| `RMAI_AlarmItem.alarm_type`  | **107** |
| `RMAI_AlarmItem.reserved[4]` | **0**   |


---



### 7.2 DSC 报警（201–206）



#### 7.2.1 alarm_type = 201 — `RMAI_ALARM_NO_DRIVER`


| 项目                           | 取值                            |
| ---------------------------- | ----------------------------- |
| 中文说明                         | 未检测到驾驶员                       |
| 输出 `alg_type`                | 6（`RMAI_ALG_DSC_FACE_DETECT`） |
| `RMAI_AlarmItem.alarm_type`  | **201**                       |
| `RMAI_AlarmItem.reserved[4]` | **0**                         |




#### 7.2.2 alarm_type = 202 — `RMAI_ALARM_FATIGUE`


| 项目                           | 取值                                                       |
| ---------------------------- | -------------------------------------------------------- |
| 中文说明                         | 驾驶员疲劳                                                    |
| 输出 `alg_type`                | 8（`RMAI_ALG_DSC_YAWN_POSE`）、9（`RMAI_ALG_DSC_EYE_STATUS`） |
| `RMAI_AlarmItem.alarm_type`  | **202**                                                  |
| `RMAI_AlarmItem.reserved[4]` | **0**                                                    |




#### 7.2.3 alarm_type = 203 — `RMAI_ALARM_DISTRACTED`


| 项目                           | 取值      |
| ---------------------------- | ------- |
| 中文说明                         | 驾驶员分心   |
| 输出 `alg_type`                | 8、9     |
| `RMAI_AlarmItem.alarm_type`  | **203** |
| `RMAI_AlarmItem.reserved[4]` | **0**   |




#### 7.2.4 alarm_type = 204 — `RMAI_ALARM_PHONE_DETECTED`


| 项目                           | 取值                      |
| ---------------------------- | ----------------------- |
| 中文说明                         | 检测到手机                   |
| 输出 `alg_type`                | 10（`RMAI_ALG_DSC_CALL`） |
| `RMAI_AlarmItem.alarm_type`  | **204**                 |
| `RMAI_AlarmItem.reserved[4]` | **0**                   |




#### 7.2.5 alarm_type = 205 — `RMAI_ALARM_NO_BELT`


| 项目                           | 取值                      |
| ---------------------------- | ----------------------- |
| 中文说明                         | 未系安全带                   |
| 输出 `alg_type`                | 22（`RMAI_ALG_DSC_BELT`） |
| `RMAI_AlarmItem.alarm_type`  | **205**                 |
| `RMAI_AlarmItem.reserved[4]` | **0**                   |




#### 7.2.6 alarm_type = 206 — `RMAI_ALARM_BLOCK_GLASS`


| 项目                           | 取值                                              |
| ---------------------------- | ----------------------------------------------- |
| 中文说明                         | 红外阻断眼镜                                          |
| 输出 `alg_type`                | 8（仅 `RMAI_ALG_DSC_YAWN_POSE`；alg_type=9 不输出本报警） |
| `RMAI_AlarmItem.alarm_type`  | **206**                                         |
| `RMAI_AlarmItem.reserved[4]` | **0**                                           |


---



### 7.3 报警汇总速查表


| alarm_type | 枚举名                                        | 中文说明    | 输出 alg_type |
| ---------- | ------------------------------------------ | ------- | ----------- |
| 0          | `RMAI_ALARM_NONE`                          | —       | 不写入 buffer  |
| 101        | `RMAI_ALARM_ADAS_FCW_COLLISION`            | 前向碰撞预警  | 2           |
| 102        | `RMAI_ALARM_ADAS_HMW_CAR_DISTANCE_CLOSE`   | 车距过近预警  | 2           |
| 103        | `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_LEFT`  | 左侧车道偏离  | 3           |
| 104        | `RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_RIGHT` | 右侧车道偏离  | 3           |
| 105        | `RMAI_ALARM_ADAS_PCW_PEDESTRIAN_COLLISION` | 行人碰撞预警  | 4           |
| 106        | `RMAI_ALARM_TSR_STOP_ALARM`                | 停车标志报警  | 26          |
| 107        | `RMAI_ALARM_TSR_SPEED_LIMIT_ALARM`         | 限速标志报警  | 26          |
| 201        | `RMAI_ALARM_NO_DRIVER`                     | 未检测到驾驶员 | 6           |
| 202        | `RMAI_ALARM_FATIGUE`                       | 驾驶员疲劳   | 8, 9        |
| 203        | `RMAI_ALARM_DISTRACTED`                    | 驾驶员分心   | 8, 9        |
| 204        | `RMAI_ALARM_PHONE_DETECTED`                | 检测到手机   | 10          |
| 205        | `RMAI_ALARM_NO_BELT`                       | 未系安全带   | 22          |
| 206        | `RMAI_ALARM_BLOCK_GLASS`                   | 红外阻断眼镜  | 8           |


---



## 8. 状态类型逐条说明（RMAI_StatusType）

所有状态均位于 `RMAI_ADAS_STATUS`**（block_type=4）**，元素 `RMAI_StatusItem`**（68 字节）**：


| 字段            | 类型         | 取值                          |
| ------------- | ---------- | --------------------------- |
| `status_type` | `uint32_t` | 下表各条 `status_type` 数值       |
| `text`        | `char[64]` | 状态文本字符串，最大 63 字符（含 `\0` 预留） |


**输出状态的算法组**：仅 **FCW/HMW（alg_type=2）** 与 **LDW（alg_type=3）**；PCW、TSR、全部 DSC 组不输出 `RMAI_ADAS_STATUS` DataBlock。

---



### 8.1 status_type = 1 — `RMAI_STATUS_ADAS_FCW_TTC`


| 项目                            | 取值              |
| ----------------------------- | --------------- |
| 中文说明                          | 前向碰撞时间 TTC      |
| 输出 `alg_type`                 | 2、3             |
| `RMAI_StatusItem.status_type` | **1**           |
| `RMAI_StatusItem.text` 示例     | `"5.0s"`（协议头注释） |


---



### 8.2 status_type = 2 — `RMAI_STATUS_ADAS_HMW_TTC`


| 项目                            | 取值         |
| ----------------------------- | ---------- |
| 中文说明                          | 车距监测时间 TTC |
| 输出 `alg_type`                 | 2、3        |
| `RMAI_StatusItem.status_type` | **2**      |
| `RMAI_StatusItem.text` 示例     | `"5.0s"`   |


---



### 8.3 status_type = 3 — `RMAI_STATUS_ADAS_CAR_SPEED`


| 项目                            | 取值         |
| ----------------------------- | ---------- |
| 中文说明                          | 当前车速       |
| 输出 `alg_type`                 | 2、3        |
| `RMAI_StatusItem.status_type` | **3**      |
| `RMAI_StatusItem.text` 示例     | `"56km/h"` |


---



### 8.4 status_type = 4 — `RMAI_STATUS_ADAS_CAR_RELATIVE_SPEED`


| 项目                            | 取值          |
| ----------------------------- | ----------- |
| 中文说明                          | 前车相对速度      |
| 输出 `alg_type`                 | 2、3         |
| `RMAI_StatusItem.status_type` | **4**       |
| `RMAI_StatusItem.text` 示例     | `"4.5km/h"` |


---



### 8.5 状态汇总速查表


| status_type | 枚举名                                   | 中文说明     | text 示例     | 输出 alg_type |
| ----------- | ------------------------------------- | -------- | ----------- | ----------- |
| 0           | `RMAI_STATUS_NONE`                    | —        | —           | 不写入         |
| 1           | `RMAI_STATUS_ADAS_FCW_TTC`            | 前向碰撞 TTC | `"5.0s"`    | 2, 3        |
| 2           | `RMAI_STATUS_ADAS_HMW_TTC`            | 车距监测 TTC | `"5.0s"`    | 2, 3        |
| 3           | `RMAI_STATUS_ADAS_CAR_SPEED`          | 当前车速     | `"56km/h"`  | 2, 3        |
| 4           | `RMAI_STATUS_ADAS_CAR_RELATIVE_SPEED` | 前车相对速度   | `"4.5km/h"` | 2, 3        |


---



## 9. 解码示例（C 语言）



### 9.1 查找指定 DataBlock

```c
#include "rmai_info_parse_struct_protocol.h"
#include <stdint.h>

static const RMAI_AlgGroupDataBlockHeader* find_data_block(
    const uint8_t* payload_base,
    const RMAI_AlgGroupPayloadHeader* ph,
    uint32_t want_type)
{
    const RMAI_AlgGroupDataBlockHeader* blocks =
        (const RMAI_AlgGroupDataBlockHeader*)(payload_base + sizeof(RMAI_AlgGroupPayloadHeader));

    for (int i = 0; i < ph->data_block_count; ++i) {
        if (blocks[i].block_type == want_type) {
            return &blocks[i];
        }
    }
    return NULL;
}
```



### 9.2 校验输出头并遍历算法组

```c
static int decode_struct_output(const RMAI_StructOutput* out)
{
    if (NULL == out || NULL == out->buffer || out->buffer_size < (int)sizeof(RMAI_AlgOutputHeader)) {
        return -1;
    }

    const RMAI_AlgOutputHeader* hdr = (const RMAI_AlgOutputHeader*)out->buffer;
    if (hdr->magic != RMAI_STRUCT_MAGIC || hdr->version != RMAI_STRUCT_VERSION) {
        return -1;
    }
    if (0 == hdr->group_count) {
        return 0;  /* 空结果 */
    }

    const RMAI_AlgGroupHeader* groups =
        (const RMAI_AlgGroupHeader*)(out->buffer + hdr->groups_offset);

    for (uint16_t i = 0; i < hdr->group_count; ++i) {
        switch (groups[i].alg_type) {
        case RMAI_ALG_FCW_HMW:
            handle_fcw_hmw(out->buffer, &groups[i]);
            break;
        case RMAI_ALG_PCW:
            handle_pcw(out->buffer, &groups[i]);
            break;
        /* 其他 alg_type 按第 6 章 DataBlock 定义处理 */
        default:
            break;
        }
    }
    return 0;
}
```



### 9.3 读取 FCW 车辆、距离、报警与状态

```c
static void handle_fcw_hmw(const uint8_t* buf,
                           const RMAI_AlgGroupHeader* item)
{
    const uint8_t* pb = buf + item->payload_offset;
    const RMAI_AlgGroupPayloadHeader* ph =
        (const RMAI_AlgGroupPayloadHeader*)pb;

    /* 目标车辆检测框 + 距离 */
    const RMAI_AlgGroupDataBlockHeader* veh_sec =
        find_data_block(pb, ph, RMAI_ADAS_VEHICLES);
    if (veh_sec && veh_sec->count > 0) {
        const RMAI_Vehicle* vehicles =
            (const RMAI_Vehicle*)(pb + veh_sec->data_offset);
        for (uint32_t k = 0; k < veh_sec->count; ++k) {
            if (!vehicles[k].is_target) continue;

            float dist_m = 0.0f;
            int has_dist = (vehicles[k].flags & 0x01) ? 1 : 0;
            if (has_dist) {
                dist_m = vehicles[k].distance;
            }
            /* vehicles[k].x/y/width/height：检测框；dist_m：米 */
        }
    }

    /* 报警 */
    const RMAI_AlgGroupDataBlockHeader* alarm_sec =
        find_data_block(pb, ph, RMAI_ALG_ALARMS);
    if (alarm_sec && alarm_sec->count > 0) {
        const RMAI_AlarmItem* alarms =
            (const RMAI_AlarmItem*)(pb + alarm_sec->data_offset);
        for (uint32_t k = 0; k < alarm_sec->count; ++k) {
            switch (alarms[k].alarm_type) {
            case RMAI_ALARM_ADAS_FCW_COLLISION:
                break;
            case RMAI_ALARM_ADAS_HMW_CAR_DISTANCE_CLOSE:
                break;
            default:
                break;
            }
        }
    }

    /* TTC / 车速等状态（text 为字符串，如 "5.0s"） */
    const RMAI_AlgGroupDataBlockHeader* status_sec =
        find_data_block(pb, ph, RMAI_ADAS_STATUS);
    if (status_sec && status_sec->count > 0) {
        const RMAI_StatusItem* st =
            (const RMAI_StatusItem*)(pb + status_sec->data_offset);
        for (uint32_t k = 0; k < status_sec->count; ++k) {
            if (st[k].status_type == RMAI_STATUS_ADAS_FCW_TTC) {
                /* st[k].text 例如 "5.0s" */
            }
        }
    }
}
```



### 9.4 读取 PCW 行人与距离

```c
static void handle_pcw(const uint8_t* buf,
                       const RMAI_AlgGroupHeader* item)
{
    const uint8_t* pb = buf + item->payload_offset;
    const RMAI_AlgGroupPayloadHeader* ph =
        (const RMAI_AlgGroupPayloadHeader*)pb;

    const RMAI_AlgGroupDataBlockHeader* ped_sec =
        find_data_block(pb, ph, RMAI_ADAS_PEDESTRIANS);
    if (ped_sec && ped_sec->count > 0) {
        const RMAI_Pedestrian* peds =
            (const RMAI_Pedestrian*)(pb + ped_sec->data_offset);
        for (uint32_t k = 0; k < ped_sec->count; ++k) {
            float dist_m = 0.0f;
            if (peds[k].flags & 0x01) {
                dist_m = peds[k].distance;
            }
            /* peds[k].x/y/width/height；is_target=1 为目标行人 */
        }
    }
}
```



### 9.5 解码要点

1. 校验 `RMAI_AlgOutputHeader`（`magic` / `version` / `total_size`）。
2. 遍历 `RMAI_AlgGroupHeader[group_count]`，按 `alg_type` 分支。
3. Payload 内**按** `block_type` **查找** DataBlock，不可假设顺序。
4. 读取 `RMAI_Vehicle` / `RMAI_Pedestrian` 的 `distance` 前必须检查 `flags & 0x01`（见 §5.5）。

---



## 10. Demo 工程（streamax_video_aiinfo_parse）



### 10.1 定位

面向客户的**参考 Demo**：演示 264/265 帧头解析、`RMAIInfo_ParseStruct` 调用与 Struct 解码。客户集成 SDK 时需随 SDK 部署 OpenCV（解析库运行时依赖）；可参考 Demo 源码，无需依赖 Demo 内 ffmpeg 视频叠加逻辑。

### 10.2 目录

```
streamax_video_aiinfo_parse/
├── rmai_info_parse_api/     # SDK
├── third_party/             # Demo 专用（ffmpeg/cryptopp）
├── src/
│   ├── main.cpp             # Create/ParseStruct 入口
│   ├── func.cpp             # 帧头解析、AI body 提取
│   └── struct_decode.cpp    # Struct 解码示例
├── build.sh / build_windows.bat
└── release/bin/             # 预编译运行包
```



### 10.3 编译与运行

编译环境须满足 **§2.1** 平台表要求（Linux：GCC 13.3.0 / glibc ≥ 2.38；Windows：VS2026 + Windows SDK 10.0.26100.0）。

```bash
./build.sh linux-release
cd release/bin/linux
./streamax_video_aiinfo_parse /path/to/video.264
```

批量脚本（需 `ADAS_video/`、`DSC_video/`）：`release/bin/run_dmoe_batch_linux.sh`。

---



## 11. 注意事项

1. 切换视频必调 `RMAIInfo_Parse_Clear()`。
2. `out.buffer` 生命周期见 §2.5。
3. `distance` 须检查 `flags & 0x01`（见 §5.5）。
4. DataBlock 顺序不固定，须按 `block_type` 查找。
5. 字节序：小端；`#pragma pack(4)`。
6. v1 **不含** Extension DataBlock。
7. `RMAI_ALG_DSC_FACE_ALIGN`（7）预留，当前版本不输出。

---