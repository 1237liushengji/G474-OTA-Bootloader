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

#endif /* __BSP_UART_H */
