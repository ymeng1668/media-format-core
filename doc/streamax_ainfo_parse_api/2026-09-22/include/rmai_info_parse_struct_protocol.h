/**
 * @file rmai_info_parse_struct_protocol.h
 * @brief RMAI AI 帧头解析 — C 变长结构体输出数据协议
 * @version 1.0.0
 *
 * 说明：
 *   - 本文件仅定义 binary buffer 布局相关的常量、枚举与结构体
 *   - 解码方只需 include 本头文件，并按协议文档自行解析 buffer
 *   - 新增算法 / DataBlock 类型时优先修改本文件
 */

#ifndef _RMAI_INFO_PARSE_STRUCT_PROTOCOL_H_
#define _RMAI_INFO_PARSE_STRUCT_PROTOCOL_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/************************************************
 * 协议常量
 ************************************************/

#define RMAI_STRUCT_MAGIC              0x524D4149u  /* 'RMAI' */
#define RMAI_STRUCT_VERSION            0x0001u
#define RMAI_STRUCT_TEXT_MAX_LEN       64
#define RMAI_STRUCT_CONTOUR_NO_PARENT  (-1)

/************************************************
 * 枚举
 ************************************************/

/** 算法类别（对应 JSON category） */
typedef enum {
    RMAI_CATEGORY_ADAS = 1,
    RMAI_CATEGORY_DSC  = 2
} RMAI_Category;

/** 算法类型（数值与 group_id 一致） */
typedef enum {
    RMAI_ALG_FCW_HMW         = 2,   /* "fcw_hmw"           */
    RMAI_ALG_LDW             = 3,   /* "ldw"               */
    RMAI_ALG_PCW             = 4,   /* "pcw"               */
    RMAI_ALG_DSC_FACE_DETECT = 6,   /* "dsc_face_detect"   */
    RMAI_ALG_DSC_FACE_ALIGN  = 7,   /* "dsc_face_align"    预留 */
    RMAI_ALG_DSC_YAWN_POSE   = 8,   /* "dsc_yawn_and_pose" */
    RMAI_ALG_DSC_EYE_STATUS  = 9,   /* "dsc_eys_status"    */
    RMAI_ALG_DSC_CALL        = 10,  /* "dsc_call"          */
    RMAI_ALG_DSC_BELT        = 22,  /* "dsc_belt"          */
    RMAI_ALG_TSR             = 26   /* "tsr"               */
} RMAI_AlgType;

/** 报警类型（对应 JSON alarms[].type） */
typedef enum {
    RMAI_ALARM_NONE = 0,

    /* ADAS */
    RMAI_ALARM_ADAS_FCW_COLLISION            = 101,
    RMAI_ALARM_ADAS_HMW_CAR_DISTANCE_CLOSE   = 102,
    RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_LEFT  = 103,
    RMAI_ALARM_ADAS_LDW_LANE_DEPARTURE_RIGHT = 104,
    RMAI_ALARM_ADAS_PCW_PEDESTRIAN_COLLISION = 105,
    RMAI_ALARM_TSR_STOP_ALARM                = 106,
    RMAI_ALARM_TSR_SPEED_LIMIT_ALARM         = 107,

    /* DSC */
    RMAI_ALARM_NO_DRIVER      = 201,
    RMAI_ALARM_FATIGUE        = 202,
    RMAI_ALARM_DISTRACTED     = 203,
    RMAI_ALARM_PHONE_DETECTED = 204,
    RMAI_ALARM_NO_BELT        = 205,
    RMAI_ALARM_BLOCK_GLASS    = 206   /* 红外阻断眼镜报警 */
} RMAI_AlarmType;

/** 状态类型（FCW/HMW status 数据块） */
typedef enum {
    RMAI_STATUS_NONE = 0,
    RMAI_STATUS_ADAS_FCW_TTC = 1,            /* JSON: "ADAS_FCW_TTC" */
    RMAI_STATUS_ADAS_HMW_TTC = 2,            /* JSON: "ADAS_HMW_TTC" */
    RMAI_STATUS_ADAS_CAR_SPEED = 3,          /* JSON: "ADAS_CAR_SPEED"，当前车速，text 如 "56km/h" */
    RMAI_STATUS_ADAS_CAR_RELATIVE_SPEED = 4  /* JSON: "ADAS_CAR_RELATIVE_SPEED"，前车相对速度，text 如 "4.5km/h" */
} RMAI_StatusType;

/** 算法组 Payload 内业务数据块类型（ADAS: 1~99，DSC: 100~） */
typedef enum {
    /* 通用 DataBlock（ADAS / DSC 各算法组共用） */
    RMAI_ALG_ALARMS                   = 3,

    /* ADAS DataBlock */
    RMAI_ADAS_VEHICLES                = 1,
    RMAI_ADAS_DETECTION_AREA          = 2,
    RMAI_ADAS_STATUS                  = 4,
    RMAI_ADAS_LEFT_LANE_POINTS        = 5,
    RMAI_ADAS_RIGHT_LANE_POINTS       = 6,
    RMAI_ADAS_PEDESTRIANS             = 7,
    RMAI_ADAS_ROLLING_STOP_COORDS     = 8,
    RMAI_ADAS_SPEED_LIMIT_SIGN_COORDS = 9,

    /* DSC DataBlock */
    RMAI_DSC_HEAD_POSE               = 100,
    RMAI_DSC_FACE_RECTS              = 101,
    RMAI_DSC_FACE_KEY_POINTS         = 102,
    RMAI_DSC_PHONE_RECTS             = 103,
    RMAI_DSC_SEATBELT_CONTOURS       = 104,
    RMAI_DSC_VECHILE_SPEED           = 105,
    RMAI_DSC_DETECTION_AREA          = 106
} RMAI_AlgDataBlockType;

/************************************************
 * 算法业务元素结构体（4 字节对齐）
 ************************************************/

#pragma pack(push, 4)

typedef struct {
    uint16_t x;
    uint16_t y;
} RMAI_Point;

typedef struct {
    uint16_t x;
    uint16_t y;
    uint16_t width;
    uint16_t height;
} RMAI_Rect;

/**
 * FCW 车辆（RMAI_ADAS_VEHICLES 数据块元素）
 * flags bit0: 含 distance 字段
 */
typedef struct {
    uint16_t x;
    uint16_t y;
    uint16_t width;
    uint16_t height;
    float    distance;
    uint8_t  is_target;
    uint8_t  flags;         /* bit0 = has_distance */
    uint8_t  reserved[2];
} RMAI_Vehicle;

/**
 * PCW 行人（RMAI_ADAS_PEDESTRIANS 数据块元素）
 * flags bit0: 含 distance 字段（由下层 Danger:1/0 + KDs: 解析写入 rect.scoreValue）
 */
typedef struct {
    uint16_t x;
    uint16_t y;
    uint16_t width;
    uint16_t height;
    float    distance;
    uint8_t  is_target;
    uint8_t  flags;         /* bit0 = has_distance */
    uint8_t  reserved[2];
} RMAI_Pedestrian;

typedef struct {
    uint32_t alarm_type;
    uint8_t  reserved[4];       /* 扩展保留，编码时填 0 */
} RMAI_AlarmItem;

typedef struct {
    uint32_t status_type;
    char     text[RMAI_STRUCT_TEXT_MAX_LEN];
} RMAI_StatusItem;

typedef struct {
    float    yaw;
    float    pitch;
    float    roll;
    uint8_t  reserved[4];
} RMAI_HeadPose;

/** DSC 车速（dsc_face_detect 内 RMAI_DSC_VECHILE_SPEED DataBlock 元素，count=1） */
typedef struct {
    int32_t  speed;         /* 车速，单位 km/h */
    uint8_t  reserved[4];
} RMAI_VechileSpeed;

typedef struct {
    int32_t  id;
    int32_t  parent_contour_id;
    uint32_t point_count;
    /* 紧跟 RMAI_Point points[point_count] */
} RMAI_ContourHeader;

/************************************************
 * 协议布局头（4 字节对齐，对应 RMFI2_VIDEOAIINFO 解析输出 buffer 分层）
 ************************************************/

/**
 * 算法解析输出包头（buffer 第 1 层）
 * 对应一次 RMAIInfo_ParseStruct 整包输出，即单个 RMFI2_VIDEOAIINFO body 的解析结果容器
 */
typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t group_count;       /* 本帧算法组个数（= RMFI2_VIDEOAIINFO 内有效 group 数） */
    uint32_t total_size;
    uint32_t groups_offset;     /* RMAI_AlgGroupHeader 数组起始偏移 */
    uint8_t  reserved[4];
} RMAI_AlgOutputHeader;

/**
 * 算法组头（buffer 第 2 层，groups[] 元素）
 * 对应帧头叠加信息内单个 group_id 的索引：时间戳、算法类型及 Payload 定位
 */
typedef struct {
    uint64_t timestamp;         /* 来自 memory.aiPts */
    int32_t  group_id;          /* 原始算法组 ID（RMFI2 叠加 groupId） */
    uint32_t alg_type;          /* RMAI_AlgType，与 group_id 同值 */
    uint16_t category;          /* RMAI_Category：ADAS / DSC */
    uint8_t  reserved[2];
    uint32_t payload_offset;    /* 本组 Payload 在 buffer 中的偏移 */
    uint32_t payload_size;      /* 本组 Payload 字节数 */
} RMAI_AlgGroupHeader;

/**
 * 算法组 Payload 头（buffer 第 3 层，Payload 起始）
 * 描述该算法组业务数据区内 DataBlock 个数
 */
typedef struct {
    uint32_t alg_type;          /* 冗余校验，与 RMAI_AlgGroupHeader.alg_type 一致 */
    uint16_t data_block_count;  /* 本组业务数据块个数 */
    uint8_t  reserved[2];
} RMAI_AlgGroupPayloadHeader;

/**
 * 算法组 DataBlock 描述头（buffer 第 3 层，Payload 内索引表项）
 * 描述组内某一类业务数据（如 vehicles / alarms）的类型、元素大小与数据偏移
 */
typedef struct {
    uint32_t block_type;        /* RMAI_AlgDataBlockType */
    uint16_t elem_size;         /* 单元素字节数；变长 DataBlock 为 0 */
    uint8_t  reserved[2];
    uint32_t count;             /* 元素个数；变长 DataBlock 为轮廓条数 */
    uint32_t data_offset;       /* 数据块内容相对 Payload 起点的偏移 */
    uint32_t data_size;         /* 数据块内容总字节数 */
} RMAI_AlgGroupDataBlockHeader;

#pragma pack(pop)

/************************************************
 * 输出描述
 ************************************************/

/**
 * ParseStruct 输出：指向库内 binary buffer
 * 调用方按协议文档 §8 自行解析，库不提供 FindDataBlock 等辅助 API
 */
typedef struct {
    const uint8_t* buffer;      /* 协议 binary 起始地址 */
    int            buffer_size; /* buffer 有效字节数 */
} RMAI_StructOutput;

#ifdef __cplusplus
}
#endif

#endif /* _RMAI_INFO_PARSE_STRUCT_PROTOCOL_H_ */
