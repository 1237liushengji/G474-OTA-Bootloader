#ifndef __LED_H
#define __LED_H

#include "main.h"

/*------------------------------------------ LED配置宏 ----------------------------------*/

#define LED1_PIN            			 	 GPIO_PIN_0        				 			// LED1 引脚（蓝色）
#define LED1_PORT           			 	 GPIOE                			 		// LED1 GPIO端口
#define __HAL_RCC_LED1_CLK_ENABLE    __HAL_RCC_GPIOE_CLK_ENABLE() 	// LED1 GPIO端口时钟

#define LED2_PIN            			 	 GPIO_PIN_1        				 			// LED2 引脚（绿色）
#define LED2_PORT           			 	 GPIOE                			 		// LED2 GPIO端口
#define __HAL_RCC_LED2_CLK_ENABLE    __HAL_RCC_GPIOE_CLK_ENABLE() 	// LED2 GPIO端口时钟
 
/*----------------------------------------- LED控制宏 ----------------------------------*/
						
#define LED1_ON 	  	HAL_GPIO_WritePin(LED1_PORT, LED1_PIN, GPIO_PIN_RESET)		// 输出低电平，点亮LED1	
#define LED1_OFF 	  	HAL_GPIO_WritePin(LED1_PORT, LED1_PIN, GPIO_PIN_SET)			// 输出高电平，关闭LED1	
#define LED1_Toggle		HAL_GPIO_TogglePin(LED1_PORT,LED1_PIN);										// 翻转IO口状态

#define LED2_ON 	  	HAL_GPIO_WritePin(LED2_PORT, LED2_PIN, GPIO_PIN_RESET)		// 输出低电平，点亮LED2	
#define LED2_OFF 	  	HAL_GPIO_WritePin(LED2_PORT, LED2_PIN, GPIO_PIN_SET)			// 输出高电平，关闭LED2	
#define LED2_Toggle		HAL_GPIO_TogglePin(LED2_PORT,LED2_PIN);										// 翻转IO口状态

/*---------------------------------------- 函数声明 ------------------------------------*/

void LED_Init(void);

#endif //__LED_H


