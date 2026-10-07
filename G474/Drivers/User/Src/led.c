#include "led.h"

void LED_Init(void)
{
	GPIO_InitTypeDef GPIO_InitStruct = {0};

	__HAL_RCC_LED1_CLK_ENABLE;		// 初始化LED1/LED2 GPIO时钟（同属GPIOE）

	HAL_GPIO_WritePin(LED1_PORT, LED1_PIN, GPIO_PIN_SET);		// LED1初始熄灭（应用层接管）
	HAL_GPIO_WritePin(LED2_PORT, LED2_PIN, GPIO_PIN_SET);		// LED2初始熄灭（总线错误指示，见main.c）

	GPIO_InitStruct.Pin 		= LED1_PIN;					// LED1引脚
	GPIO_InitStruct.Mode 	= GPIO_MODE_OUTPUT_PP;	// 推挽输出模式
	GPIO_InitStruct.Pull 	= GPIO_NOPULL;				// 不上下拉
	GPIO_InitStruct.Speed 	= GPIO_SPEED_FREQ_LOW;	// 低速模式
	HAL_GPIO_Init(LED1_PORT, &GPIO_InitStruct);

	GPIO_InitStruct.Pin 		= LED2_PIN;					// LED2引脚
	GPIO_InitStruct.Mode 	= GPIO_MODE_OUTPUT_PP;	// 推挽输出模式
	GPIO_InitStruct.Pull 	= GPIO_NOPULL;				// 不上下拉
	GPIO_InitStruct.Speed 	= GPIO_SPEED_FREQ_LOW;	// 低速模式
	HAL_GPIO_Init(LED2_PORT, &GPIO_InitStruct);			// 注意：LED2端口

}
