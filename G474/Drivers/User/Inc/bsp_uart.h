/**
  ******************************************************************************
  * @file    bsp_uart.h
  * @brief   控制台串口驱动（USART1，接板上USB转串口，PC上位机经此访问）
  *
  * 引脚说明：默认按USART1的官方引脚 PA9=TX / PA10=RX（AF7）配置。
  * 若你的板卡USB转串口接的不是这两个脚，仅需修改下方4个宏后重编译。
  ******************************************************************************
  */
#ifndef __BSP_UART_H
#define __BSP_UART_H

#include "main.h"
#include <stdint.h>

/*--------------------------------------- 引脚与参数配置 --------------------------------------*/
#define BSP_CONSOLE_USART           USART1
#define BSP_CONSOLE_USART_IRQn      USART1_IRQn
#define BSP_CONSOLE_CLK_ENABLE      __HAL_RCC_USART1_CLK_ENABLE
#define BSP_CONSOLE_TX_PORT         GPIOA
#define BSP_CONSOLE_TX_PIN          GPIO_PIN_9      /* PA9  = USART1_TX */
#define BSP_CONSOLE_RX_PORT         GPIOA
#define BSP_CONSOLE_RX_PIN          GPIO_PIN_10     /* PA10 = USART1_RX */
#define BSP_CONSOLE_GPIO_AF         GPIO_AF7_USART1
#define BSP_CONSOLE_BAUDRATE        115200U
#define BSP_CONSOLE_RX_RING_SIZE    128U            /* 接收环形缓冲深度 */

/*--------------------------------------- 外部变量 ---------------------------------------*/
extern UART_HandleTypeDef huart1;

/*--------------------------------------- 函数声明 ---------------------------------------*/
void     BSP_UART_Init(void);
void     BSP_UART_Send(const char *data, uint16_t len);
void     BSP_UART_Printf(const char *fmt, ...);
uint16_t BSP_UART_Available(void);
int16_t  BSP_UART_GetChar(void);
/* 【2026-10-09 真机诊断】接收链路计数：前者应随 PC 键入递增。
 * 恒为 0 而"PC 能收到本机输出" => RX 方向断（NVIC/USART/PA10 接线或复用）。 */
uint32_t BSP_UART_RxIrqCount(void);
uint32_t BSP_UART_RxDropCount(void);
/* 接收链路自诊断（RX 收不到时周期性打印寄存器快照，见 .c 内说明） */
void     BSP_UART_DiagTask(void);

/* 【2026-10-11】PA10(RX) 引脚累计跳变监视器：
 * 由主循环高频调用，维护"自开机以来 PA10 跳变总数"。
 * 该计数 > 0 即证明外部信号真的到达过 PA10 引脚（无观测偏差判据）。 */
void     BSP_UART_RxPinPulse(void);
uint32_t BSP_UART_RxPinPulseCnt(void);

/* 【2026-10-11】全端口引脚跳变监视器（定位"CH340K 的 TXD 到底接到哪个引脚"）。
 * 非侵入：只读 GPIOA..GPIOE 的 IDR，累积"哪些位曾经跳变过"及其次数。
 * 在"PC 长发送 + MCU 存活"的重叠窗口内，除已知的 PA9/PA13/PA14/PB12/PB13/PE0/PE1
 * 之外出现的任何跳变位 = 外部器件驱动的引脚。 */
#define BSP_PINMON_PORT_NUM     5U              /* GPIOA..GPIOE */
void     BSP_UART_PinMonitor(void);
void     BSP_UART_PinMonitorDump(void);

/* 【2026-10-11】PA10 外部连通性判据（内部下拉探测法，静态电平、无窗口偏差）：
 *  返回 1 = 外部有强驱动把 PA10 拉高 ⟹ CH340K TXD 与 PA10 连通
 *  返回 0 = 被内部下拉拉低       ⟹ 外部无驱动 ⟹ 走线开路
 * 注意：本函数会临时把 PA10 切成输入/下拉，随后自动恢复为 USART1_RX。 */
uint8_t  BSP_UART_ProbeRxPinConnected(void);
uint8_t  BSP_UART_RxPinProbeResult(void);
uint8_t  BSP_UART_RxPinProbePullUp(void);
/* 边沿检测灵敏度自检：先证明"能捕获 115200 波特率信号"，阴性结论才有意义 */
void     BSP_UART_EdgeDetectSelfTest(void);

/* 应急输出：直接写 USART1 寄存器，不经 HAL、不依赖中断。
 * 供 HardFault_Handler / Error_Handler / 启动打点使用。 */
void     BSP_UART_EmergencyPuts(const char *s);
void     BSP_UART_EmergencyHex(const char *tag, uint32_t value);
void     BSP_UART_BootMark(const char *msg);
void     BSP_UART_FaultDump(const char *tag);
/* 打印异常栈帧的 fault PC/LR/xPSR（逐偏移搜索帧底，sp 由调用方在入口处取得）。
 * 本次排查 USB CDC 的 MemManage 故障时，CFSR/HFSR 均为 0，只有它能定位到指令。 */
void     BSP_UART_PrintExcFrame(uint32_t sp);

/* 【2026-10-12】TFT 滚动日志：串口不可用时的板上可观测通道。
 * 所有经 BSP_UART_Send() 输出的文本会按行存入环形缓冲，
 * 由 BSP_UART_LcdLogRender() 在主循环里按 1Hz 刷到屏幕第 10..16 行。 */
void     BSP_UART_LcdLogEnable(uint8_t enable);
void     BSP_UART_LcdLogRender(void);
uint8_t  BSP_UART_LcdLogTotal(void);

#endif /* __BSP_UART_H */
