/**
  ******************************************************************************
  * @file    can.h
  * @brief   双机CAN通信驱动（基于FDCAN2，经典CAN模式）
  *
  * 硬件平台：STM32G474VET6 开发板 + SN65HVD230 收发器模块
  * 引脚分配（依据板卡丝印/引脚图：CAN_RX->PB12、CAN_TX->PB13）：
  *           PB12 = FDCAN2_RX（AF9，接模块CTX/TXD引脚）
  *           PB13 = FDCAN2_TX（AF9，接模块CRX/RXD引脚）
  *           （PB12/PB13为G474的FDCAN2复用引脚，数据手册Table 13）
  *
  * 位时序（经典CAN 500 kbit/s，采样点 75%）：
  *           FDCAN内核时钟 = HSE 8 MHz（见 stm32g4xx_hal_msp.c）
  *           NominalPrescaler = 1  ->  位量子 tq = 125 ns
  *           1位 = 16 tq = 2 us     ->  500 kbit/s
  *           同步段(1) + Seg1(11) + Seg2(4)，采样点 = (1+11)/16 = 75%
  *           （2026-10-11 修正：此前注释写的是 PCLK1=150MHz / 10tq / 80%，已过时）
  ******************************************************************************
  * @attention
  * 参考实现：STMicroelectronics/STM32CubeG4 官方示例
  *           Projects/STM32G474E-EVAL/Examples/FDCAN/FDCAN_Classic_Frame_Networking
  ******************************************************************************
  */
#ifndef __CAN_H
#define __CAN_H

#include "main.h"
#include <stdbool.h>
#include <stdint.h>

#define CAN_APP_VERSION                "3.0.0"       /* 应用固件版本（version命令上报） */

/*--------------------------------------- 节点角色配置 --------------------------------------*
 * 双机通信两个节点烧录不同固件：                                                          *
 *   板A（主节点）：周期发送命令帧，监视链路状态                                            *
 *   板B（从节点）：接收命令帧、翻转LED并应答                                              *
 * 使用方法：烧录板A固件时保持 CAN_NODE_ROLE = CAN_NODE_A；                                *
 *           烧录板B固件时改为 CAN_NODE_ROLE = CAN_NODE_B 后重新编译。                      */
#define CAN_NODE_A                     0
#define CAN_NODE_B                     1

/* 【2026-10-12】改为可被构建定义覆盖：板间 OTA 需要一块 A(发 UDS 请求)、
 * 一块 B(收并刷写)。构建板 B 时在 Keil 的 Define 里加
 *     CAN_NODE_ROLE=CAN_NODE_B   和   APP_BOARD_FP=FP_B2
 * 即可，无需改源码。注意 Keil 的 <Define> 里不要写带引号的字符串。 */
#ifndef CAN_NODE_ROLE
#define CAN_NODE_ROLE                  CAN_NODE_A    /* 默认：节点 A(master) */
#endif

/* 单板自测试（硬件排错用）：置1后FDCAN进入外部回环模式——本板发出的帧会被
 * 自己收到（0x321按从节点逻辑应答，0x322按主节点逻辑处理），单板即可完成
 * "LED同步闪烁"全流程。用于把固件问题与接线问题隔离开：
 * 自测试能同步闪烁 => 固件正常，问题在外部总线/收发器/接线。
 * 测试完毕必须改回0恢复双机正常模式！测试时CAN_NODE_ROLE须为CAN_NODE_A。 */
#define CAN_DEBUG_SELFTEST             0

/*--------------------------------------- 总线参数 ---------------------------------------*/
#define CAN_BITRATE                    500000U       /* 总线波特率 500 kbit/s（经典CAN） */
#define CAN_LINK_TIMEOUT_MS            1500U         /* 链路超时判定时间（无报文视为断链） */

/*--------------------------------------- 应用层协议 --------------------------------------*
 * 命令帧（板A -> 板B）：标准ID 0x321，数据长度8字节
 *   data[0]      : 序号seq，每发一帧自增1（回绕），用于丢帧统计
 *   data[1..2]   : 板A开机秒数（小端）
 *   data[3..7]   : 保留0x00
 *
 * 应答帧（板B -> 板A）：标准ID 0x322，数据长度8字节
 *   data[0]      : 回显触发本应答的命令帧序号seq
 *   data[1]      : 板B当前LED状态（0灭/1亮）
 *   data[2..3]   : 板B累计收到的命令帧数（小端）
 *   data[4..7]   : 保留0x00                                                              */
#define CAN_ID_CMD_A2B                 0x321U
#define CAN_ID_RESP_B2A                0x322U
#define CAN_PAYLOAD_LEN                8U

/*--------------------------------------- 类型定义 ---------------------------------------*/
typedef struct
{
  uint32_t id;                              /* 报文标准ID */
  uint32_t timestamp;                       /* 接收时间戳计数（未启用时为0） */
  uint8_t  len;                             /* 有效数据长度（0~8） */
  uint8_t  data[CAN_PAYLOAD_LEN];           /* 数据域 */
} CAN_RxMsg;

typedef struct
{
  volatile uint32_t txCount;                /* 成功写入Tx FIFO的帧数 */
  volatile uint32_t txAckCount;             /* 经Tx Event FIFO确认送达总线的帧数 */
  volatile uint32_t txMaxLatencyMs;         /* 入队到总线确认的最大延迟（ms） */
  volatile uint32_t rxCount;                /* 成功接收的帧数 */
  volatile uint32_t txErrors;               /* 发送失败次数（FIFO满等） */
  volatile uint32_t rxOverruns;             /* 接收环形缓冲覆盖/报文丢失次数 */
  volatile uint32_t busOffCount;            /* 总线关闭（bus-off）发生次数 */
  volatile uint32_t errPassiveCount;        /* 进入/退出错误被动状态事件次数 */
  volatile uint32_t txEvtLost;              /* Tx事件FIFO元素丢失次数（正常应为0） */
  volatile uint32_t protocolErrors;         /* 协议类错误中断次数 */
} CAN_Stats;

/*--------------------------------------- 外部变量 ---------------------------------------*/
extern FDCAN_HandleTypeDef hfdcan2;

/*--------------------------------------- 函数声明 ---------------------------------------*/
HAL_StatusTypeDef CAN_Init(void);
HAL_StatusTypeDef CAN_Send(uint32_t stdId, const uint8_t *data, uint8_t len);
bool              CAN_PollRx(CAN_RxMsg *msg);
const CAN_Stats  *CAN_GetStats(void);

/* 运行时重配置（控制台调用）：
 * 波特率仅支持125k/250k/500k/1M（内部为预计算时序表，保证所有档位
 * 统一16tq/位、采样点75%）；两块板必须同步切换，否则无法互通。
 * 混杂模式（总线监视sniff）：接收总线上所有ID的报文。 */
HAL_StatusTypeDef CAN_SetBitrate(uint32_t bitrate);
HAL_StatusTypeDef CAN_SetPromiscuous(bool enable);
uint32_t          CAN_GetBitrate(void);

/* 驱动工程化（阶段1）：
 * CAN_Task   —— 主循环周期调用：调度bus-off指数退避恢复（1s起步翻倍，
 *               上限30s；任一帧发送确认成功即复位退避），避免错误状态下
 *               立即重连风暴；
 * CAN_SelfTest —— 上电自检：临时切入内部回环自发自收一帧测试报文，
 *               验证"FDCAN内核+消息RAM+中断+收发路径"软件链路完整，
 *               不驱动总线引脚（内部回环含总线监测模式）。 */
void CAN_Task(void);
bool CAN_SelfTest(void);

/*--------------------------------------- 无串口诊断（2026-10-11 新增）---------------
 * 背景：P2（串口 RX 不通 + CH340 DTR/RTS 接 NRST）使串口取证不可靠，
 *       因此提供一组 volatile 全局量，用 SWD 直接读 RAM 即可判定 CAN 状态。
 * 用法：
 *   1) Keil 编译后从 .map 查 CAN_DiagSnap / CAN_DiagStage 的符号地址
 *   2) STM32_Programmer_CLI -c port=SWD sn=8 mode=UR -r32 <addr> <words>
 * 全程不碰串口，绕开 P2 的所有干扰。
 * ----------------------------------------------------------------------------- */
typedef struct
{
  volatile uint32_t magic;        /* 0xCA9D1A60 = 本结构已被填写 */
  volatile uint32_t stage;        /* 阶段码：见 CAN_DiagStage_e */
  volatile uint32_t tick_before;  /* CAN_Init 前 HAL_GetTick() */
  volatile uint32_t tick_after;   /* CAN_Init 后 HAL_GetTick()  */
  volatile uint32_t init_status;  /* CAN_Init 返回码（0=HAL_OK） */
  volatile uint32_t selftest_ok;  /* CAN_SelfTest 返回（0/1） */
  volatile uint32_t cccr;         /* FDCAN2->CCCR  */
  volatile uint32_t psr;          /* FDCAN2->PSR   */
  volatile uint32_t nbtp;         /* FDCAN2->NBTP  ← 位时序是否被写入的关键 */
  volatile uint32_t txbc;         /* FDCAN2->TXBC  ← TFQM 位 */
  volatile uint32_t txfqs;        /* FDCAN2->TXFQS */
  volatile uint32_t txbrp;        /* FDCAN2->TXBRP */
  volatile uint32_t msg_ram_a750; /* FDCAN2 消息RAM 基址可读性探针 */
  volatile uint32_t dbgmcu_idcode;/* 0xE0042000：DEV_ID + REV_ID ← 用于查 errata */
} CAN_DiagSnap_t;

/* 阶段码 */
#define CAN_DIAG_STAGE_IDLE        0U   /* 未开始 */
#define CAN_DIAG_STAGE_TICK_OK     1U   /* tick 在走（前置检查通过） */
#define CAN_DIAG_STAGE_INIT_ENTER  2U   /* 即将调 CAN_Init */
#define CAN_DIAG_STAGE_INIT_DONE   3U   /* CAN_Init 已返回 */
#define CAN_DIAG_STAGE_SELFTEST    4U   /* 自检已完成 */

/* 【2026-10-18】把 FDCAN RX FIFO0 里已到达的帧搬进软件环形缓冲。
 * 供"无法调用 CAN_Task 的阻塞循环"主动泵用（如 UART 等待、Flash 编程）。
 * CAN 未初始化时安全返回。 */
void CAN_PumpRx(void);

extern CAN_DiagSnap_t g_canDiag;

/* 【2026-10-18】发送停滞看门狗触发次数：>0 说明总线曾长时间无法送达帧
 * （对端不 ACK / 位时序不匹配 / 总线断开），已被主动 Abort 兜底。 */
extern volatile uint32_t g_canTxStallAborts;
void CAN_DiagCapture(uint32_t stage);

/*--------------------------------------- 载荷编解码 --------------------------------------*/
/**
  * @brief  编码命令帧载荷（板A发送）
  */
static inline void CAN_EncodeCmd(uint8_t *p, uint8_t seq, uint16_t uptimeSec)
{
  p[0] = seq;
  p[1] = (uint8_t)(uptimeSec & 0xFFU);
  p[2] = (uint8_t)((uptimeSec >> 8) & 0xFFU);
  p[3] = 0U;
  p[4] = 0U;
  p[5] = 0U;
  p[6] = 0U;
  p[7] = 0U;
}

/**
  * @brief  编码应答帧载荷（板B发送）
  */
static inline void CAN_EncodeResp(uint8_t *p, uint8_t seq, uint8_t ledState, uint16_t rxCmdCount)
{
  p[0] = seq;
  p[1] = ledState;
  p[2] = (uint8_t)(rxCmdCount & 0xFFU);
  p[3] = (uint8_t)((rxCmdCount >> 8) & 0xFFU);
  p[4] = 0U;
  p[5] = 0U;
  p[6] = 0U;
  p[7] = 0U;
}

#endif /* __CAN_H */
