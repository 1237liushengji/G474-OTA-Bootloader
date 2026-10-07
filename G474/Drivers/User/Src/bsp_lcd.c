/**
  ******************************************************************************
  * @file    bsp_lcd.c
  * @brief   Soft-SPI TFT LCD driver (ST7789 / ILI9341 / ST7735).
  *
  *  Why soft SPI: the W25Q128 owns the hardware SPI1 pins (PA5/6/7) while
  *  the LCD uses a second pin group (PB3/PB5); a bit-banged bus keeps both
  *  independent and the code self-contained.
  *
  *  Font: 5x7 ASCII table (columns, MSB = top), cell = 6x8 px.
  ******************************************************************************
  */
#include "bsp_lcd.h"
#include "app_board_cfg.h"

/*====================================================================*/
/* Controller geometry & config                                        */
/*====================================================================*/
/* LCD_CTRL_SELECT defined in bsp_board_cfg.h:
   0 = ST7789 240x280 (1.69 inch, GRAM 240x320, visible y-offset 20)
   1 = ILI9341 (240x320), 2 = ST7735 (128x160)
   Geometry & offsets are tuned for the Genbotter 1.69" 240x280 panel. */

#ifndef LCD_CTRL_SELECT
#define LCD_CTRL_SELECT   0
#endif

#if   (LCD_CTRL_SELECT == 0)
const uint16_t LCD_WIDTH  = 240;      /* portrait: 240 px wide            */
const uint16_t LCD_HEIGHT = 280;      /* visible 280 px of the 320 GRAM   */
#define LCD_X_OFFSET   0U             /* controller X origin offset       */
#define LCD_Y_OFFSET   20U            /* panel starts 20 px into GRAM     */
#define LCD_MADCTL_VAL 0x00U          /* MY only: flip vertical (fixes the
                                         upside-down seen with 0x00 while
                                         keeping left/right order)        */
#elif (LCD_CTRL_SELECT == 1)
const uint16_t LCD_WIDTH  = 240;
const uint16_t LCD_HEIGHT = 320;
#define LCD_X_OFFSET   0U
#define LCD_Y_OFFSET   0U
#define LCD_MADCTL_VAL 0x40U          /* landscape for ILI9341            */
#else
const uint16_t LCD_WIDTH  = 128;
const uint16_t LCD_HEIGHT = 160;
#define LCD_X_OFFSET   0U
#define LCD_Y_OFFSET   0U
#define LCD_MADCTL_VAL 0x00U
#endif

/* font scale: 5x7 glyph drawn at 2x -> 10x14 + 2px cell gap => 12x16 */
#define LCD_FONT_SCALE      2U
#define LCD_CHAR_CELL_W     (6U * LCD_FONT_SCALE)   /* 12 px */
#define LCD_CHAR_CELL_H     (8U * LCD_FONT_SCALE)   /* 16 px */

const uint8_t LCD_CHAR_COLS = (uint8_t)(LCD_WIDTH / LCD_CHAR_CELL_W);  /* 20 */
const uint8_t LCD_CHAR_ROWS = (uint8_t)(LCD_HEIGHT / LCD_CHAR_CELL_H); /* 17 */

/*====================================================================*/
/* Soft SPI primitives (pins come from bsp_board_cfg.h)
 *
 * Performance: HAL_GPIO_WritePin() is slow (~50ns+ per call with arg
 * checking). The screen is a soft-SPI device and whole-screen fills push
 * 240x280x16 = ~1M bits, so we write GPIO registers directly (BSRR/BRR).
 * SCL(PB3) and SDA(PB5) share GPIOB -> can even be set in one BSRR write.
 * CS/DC/BL are only toggled rarely (per byte / per command), so they keep
 * the simple macro form but still use register access for speed.           */
#define LCD_CS_LOW()   (LCD_CS_PORT->BRR  = LCD_CS_PIN)
#define LCD_CS_HIGH()  (LCD_CS_PORT->BSRR = LCD_CS_PIN)
#define LCD_SCL_LOW()  (LCD_SCL_PORT->BRR  = LCD_SCL_PIN)
#define LCD_SCL_HIGH() (LCD_SCL_PORT->BSRR = LCD_SCL_PIN)
#define LCD_SDA_LOW()  (LCD_SDA_PORT->BRR  = LCD_SDA_PIN)
#define LCD_SDA_HIGH() (LCD_SDA_PORT->BSRR = LCD_SDA_PIN)
#define LCD_DC_LOW()   (LCD_DC_PORT->BRR  = LCD_DC_PIN)
#define LCD_DC_HIGH()  (LCD_DC_PORT->BSRR = LCD_DC_PIN)
#define LCD_BL_ON()    (LCD_BL_PORT->BSRR = LCD_BL_PIN)
#define LCD_BL_OFF()   (LCD_BL_PORT->BRR  = LCD_BL_PIN)

static void LCD_WriteByte(uint8_t dat)
{
    uint8_t i;

    for (i = 0U; i < 8U; i++)
    {
        /* SPI mode 0: SCL idle low; SDA settles while SCL low, then the
           rising edge of SCL samples it. */
        LCD_SCL_PORT->BRR = (uint32_t)LCD_SCL_PIN;          /* SCL low  */
        if ((dat & 0x80U) != 0U)
        {
            LCD_SDA_PORT->BSRR = (uint32_t)LCD_SDA_PIN;     /* SDA=1 */
        }
        else
        {
            LCD_SDA_PORT->BRR = (uint32_t)LCD_SDA_PIN;       /* SDA=0 */
        }
        __NOP(); __NOP(); __NOP();    /* data setup time */
        LCD_SCL_PORT->BSRR = (uint32_t)LCD_SCL_PIN;         /* SCL high: sample */
        __NOP(); __NOP();             /* hold time */
        dat <<= 1U;
    }
}

static void LCD_WriteCmd(uint8_t cmd)
{
    LCD_DC_LOW();
    LCD_WriteByte(cmd);
    LCD_DC_HIGH();
}

static void LCD_WriteData(uint8_t dat)
{
    LCD_WriteByte(dat);
}

static void LCD_WriteData16(uint16_t dat)
{
    LCD_WriteData((uint8_t)(dat >> 8));
    LCD_WriteData((uint8_t)(dat & 0xFFU));
}

/*====================================================================*/
/* Init sequences                                                      */
/*====================================================================*/
static void LCD_InitSequence(void)
{
#if   (LCD_CTRL_SELECT == 0)   /* ST7789 240x280 - mirror vendor example */
    LCD_WriteCmd(0x36); LCD_WriteData(LCD_MADCTL_VAL);  /* MADCTL portrait */
    LCD_WriteCmd(0x3A); LCD_WriteData(0x05);          /* COLMOD 16bit */
    LCD_WriteCmd(0xB2); LCD_WriteData(0x0C); LCD_WriteData(0x0C);
                        LCD_WriteData(0x00); LCD_WriteData(0x33);
                        LCD_WriteData(0x33);
    LCD_WriteCmd(0xB7); LCD_WriteData(0x35);          /* VGH/VGL */
    LCD_WriteCmd(0xBB); LCD_WriteData(0x19);          /* VCOM */
    LCD_WriteCmd(0xC0); LCD_WriteData(0x2C);
    LCD_WriteCmd(0xC2); LCD_WriteData(0x01);
    LCD_WriteCmd(0xC3); LCD_WriteData(0x12);          /* VRH */
    LCD_WriteCmd(0xC4); LCD_WriteData(0x20);          /* VDV */
    LCD_WriteCmd(0xC6); LCD_WriteData(0x0F);          /* 60 Hz */
    LCD_WriteCmd(0xD0); LCD_WriteData(0xA4); LCD_WriteData(0xA1);
    /* vendor gamma */
    LCD_WriteCmd(0xE0); LCD_WriteData(0xD0); LCD_WriteData(0x04);
                        LCD_WriteData(0x0D); LCD_WriteData(0x11);
                        LCD_WriteData(0x13); LCD_WriteData(0x2B);
                        LCD_WriteData(0x3F); LCD_WriteData(0x54);
                        LCD_WriteData(0x4C); LCD_WriteData(0x18);
                        LCD_WriteData(0x0D); LCD_WriteData(0x0B);
                        LCD_WriteData(0x1F); LCD_WriteData(0x23);
    LCD_WriteCmd(0xE1); LCD_WriteData(0xD0); LCD_WriteData(0x04);
                        LCD_WriteData(0x0C); LCD_WriteData(0x11);
                        LCD_WriteData(0x13); LCD_WriteData(0x2C);
                        LCD_WriteData(0x3F); LCD_WriteData(0x44);
                        LCD_WriteData(0x51); LCD_WriteData(0x2F);
                        LCD_WriteData(0x1F); LCD_WriteData(0x1F);
                        LCD_WriteData(0x20); LCD_WriteData(0x23);
    LCD_WriteCmd(0x21);                 /* INVON: panel is normally black */
    LCD_WriteCmd(0x11);                 /* SLPOUT */
    HAL_Delay(120);
    LCD_WriteCmd(0x29);                 /* DISPON */
    HAL_Delay(50);

#elif (LCD_CTRL_SELECT == 1)   /* ILI9341 */
    LCD_WriteCmd(0x01);
    HAL_Delay(120);
    LCD_WriteCmd(0x11);
    HAL_Delay(120);
    LCD_WriteCmd(0x36); LCD_WriteData(0x48);          /* MADCTL MV */
    LCD_WriteCmd(0x3A); LCD_WriteData(0x55);
    LCD_WriteCmd(0xC0); LCD_WriteData(0x23);
    LCD_WriteCmd(0xC1); LCD_WriteData(0x10);
    LCD_WriteCmd(0xC5); LCD_WriteData(0x3E); LCD_WriteData(0x28);
    LCD_WriteCmd(0xC7); LCD_WriteData(0x86);
    LCD_WriteCmd(0x36); LCD_WriteData(0x48);
    LCD_WriteCmd(0x3A); LCD_WriteData(0x55);
    LCD_WriteCmd(0xB1); LCD_WriteData(0x00); LCD_WriteData(0x18);
    LCD_WriteCmd(0xB6); LCD_WriteData(0x08); LCD_WriteData(0x82);
                        LCD_WriteData(0x27);
    LCD_WriteCmd(0x26); LCD_WriteData(0x01);
    LCD_WriteCmd(0xE0); LCD_WriteData(0x0F); LCD_WriteData(0x1A);
                        LCD_WriteData(0x18); LCD_WriteData(0x0A);
                        LCD_WriteData(0x0F); LCD_WriteData(0x08);
                        LCD_WriteData(0x0A); LCD_WriteData(0x45);
                        LCD_WriteData(0x46); LCD_WriteData(0x08);
                        LCD_WriteData(0x0B); LCD_WriteData(0x0D);
                        LCD_WriteData(0x30); LCD_WriteData(0x37);
                        LCD_WriteData(0x0F);
    LCD_WriteCmd(0xE1); LCD_WriteData(0x00); LCD_WriteData(0x19);
                        LCD_WriteData(0x1B); LCD_WriteData(0x04);
                        LCD_WriteData(0x10); LCD_WriteData(0x07);
                        LCD_WriteData(0x08); LCD_WriteData(0x03);
                        LCD_WriteData(0x03); LCD_WriteData(0x07);
                        LCD_WriteData(0x0D); LCD_WriteData(0x13);
                        LCD_WriteData(0x1D); LCD_WriteData(0x20);
                        LCD_WriteData(0x00);
    LCD_WriteCmd(0x29);
    HAL_Delay(100);

#else   /* ST7735 */
    LCD_WriteCmd(0x01);
    HAL_Delay(150);
    LCD_WriteCmd(0x11);
    HAL_Delay(120);
    LCD_WriteCmd(0xB1); LCD_WriteData(0x01); LCD_WriteData(0x2C);
                        LCD_WriteData(0x2D);
    LCD_WriteCmd(0xB2); LCD_WriteData(0x01); LCD_WriteData(0x2C);
                        LCD_WriteData(0x2D);
    LCD_WriteCmd(0xB3); LCD_WriteData(0x01); LCD_WriteData(0x2C);
                        LCD_WriteData(0x2D); LCD_WriteData(0x01);
                        LCD_WriteData(0x2C); LCD_WriteData(0x2D);
    LCD_WriteCmd(0xB4); LCD_WriteData(0x07);
    LCD_WriteCmd(0xC0); LCD_WriteData(0xA2); LCD_WriteData(0x02);
                        LCD_WriteData(0x84);
    LCD_WriteCmd(0xC1); LCD_WriteData(0xC5);
    LCD_WriteCmd(0xC2); LCD_WriteData(0x0A); LCD_WriteData(0x00);
    LCD_WriteCmd(0xC3); LCD_WriteData(0x8A); LCD_WriteData(0x2A);
    LCD_WriteCmd(0xC4); LCD_WriteData(0x8A); LCD_WriteData(0xEE);
    LCD_WriteCmd(0xC5); LCD_WriteData(0x0E);
    LCD_WriteCmd(0x20); LCD_WriteData(0x00);
    LCD_WriteCmd(0x21);
    LCD_WriteCmd(0x3A); LCD_WriteData(0x05);
    LCD_WriteCmd(0x36); LCD_WriteData(0xC8);          /* MADCTL */
    LCD_WriteCmd(0xE0); LCD_WriteData(0x02); LCD_WriteData(0x1C);
                        LCD_WriteData(0x07); LCD_WriteData(0x12);
                        LCD_WriteData(0x37); LCD_WriteData(0x32);
                        LCD_WriteData(0x29); LCD_WriteData(0x2D);
                        LCD_WriteData(0x29); LCD_WriteData(0x25);
                        LCD_WriteData(0x2B); LCD_WriteData(0x39);
                        LCD_WriteData(0x00); LCD_WriteData(0x01);
                        LCD_WriteData(0x03); LCD_WriteData(0x10);
    LCD_WriteCmd(0xE1); LCD_WriteData(0x03); LCD_WriteData(0x1D);
                        LCD_WriteData(0x07); LCD_WriteData(0x06);
                        LCD_WriteData(0x2E); LCD_WriteData(0x2C);
                        LCD_WriteData(0x29); LCD_WriteData(0x2D);
                        LCD_WriteData(0x2E); LCD_WriteData(0x2E);
                        LCD_WriteData(0x2F); LCD_WriteData(0x3F);
                        LCD_WriteData(0x00); LCD_WriteData(0x00);
                        LCD_WriteData(0x02); LCD_WriteData(0x10);
    LCD_WriteCmd(0x29);
    HAL_Delay(100);
#endif
}

/*====================================================================*/
/* Geometry helpers                                                    */
/*====================================================================*/
static void LCD_SetWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    LCD_WriteCmd(0x2A);                 /* CASET */
    LCD_WriteData16((uint16_t)(x0 + LCD_X_OFFSET));
    LCD_WriteData16((uint16_t)(x1 + LCD_X_OFFSET));
    LCD_WriteCmd(0x2B);                 /* RASET */
    LCD_WriteData16((uint16_t)(y0 + LCD_Y_OFFSET));
    LCD_WriteData16((uint16_t)(y1 + LCD_Y_OFFSET));
    LCD_WriteCmd(0x2C);                 /* RAMWR */
}

static void LCD_PushPixel(uint16_t color)
{
    LCD_WriteData16(color);
}

/*====================================================================*/
/* Public API                                                          */
/*====================================================================*/
void LCD_Init(void)
{
    GPIO_InitTypeDef gpio = {0};

    LCD_GPIO_CLK_ENABLE();

    gpio.Mode  = GPIO_MODE_OUTPUT_PP;
    gpio.Pull  = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_HIGH;

    /* NOTE: init each pin on its OWN port. OR-ing all PIN masks together
       and calling HAL_GPIO_Init on every port would wrongly reconfigure
       unrelated pins - e.g. PB11 (USART3_RX / RS485!) got clobbered here,
       which broke slave RX. Each line below configures exactly one pin. */
    gpio.Pin = LCD_CS_PIN;
    HAL_GPIO_Init(LCD_CS_PORT, &gpio);

    gpio.Pin = LCD_SCL_PIN;
    HAL_GPIO_Init(LCD_SCL_PORT, &gpio);

    gpio.Pin = LCD_SDA_PIN;
    HAL_GPIO_Init(LCD_SDA_PORT, &gpio);

    gpio.Pin = LCD_DC_PIN;
    HAL_GPIO_Init(LCD_DC_PORT, &gpio);

    gpio.Pin = LCD_BL_PIN;
    HAL_GPIO_Init(LCD_BL_PORT, &gpio);

    LCD_CS_HIGH();
    LCD_SCL_HIGH();
    LCD_BL_OFF();

    HAL_Delay(20);
    LCD_BL_ON();

    LCD_CS_LOW();
    LCD_InitSequence();
    LCD_CS_HIGH();

    LCD_Fill(LCD_COLOR_BLACK);
}

void LCD_Fill(uint16_t color)
{
    uint32_t total = (uint32_t)LCD_WIDTH * LCD_HEIGHT;

    LCD_CS_LOW();
    LCD_SetWindow(0, 0, (uint16_t)(LCD_WIDTH - 1U), (uint16_t)(LCD_HEIGHT - 1U));
    while (total-- > 0U)
    {
        LCD_PushPixel(color);
    }
    LCD_CS_HIGH();
}

void LCD_Clear(void)
{
    LCD_Fill(LCD_COLOR_BLACK);
}

void LCD_ClearRow(uint8_t row, uint16_t color)
{
    uint32_t total = (uint32_t)LCD_WIDTH * LCD_CHAR_CELL_H;
    uint16_t x = 0U;
    uint16_t y = (uint16_t)row * LCD_CHAR_CELL_H;

    if (row >= LCD_CHAR_ROWS)
    {
        return;
    }
    LCD_CS_LOW();
    LCD_SetWindow(x, y, (uint16_t)(LCD_WIDTH - 1U),
                  (uint16_t)(y + LCD_CHAR_CELL_H - 1U));
    while (total-- > 0U)
    {
        LCD_PushPixel(color);
    }
    LCD_CS_HIGH();
}

/*====================================================================*/
/* 5x7 ASCII font, column-major: each byte = one column, bit0 = TOP row
   (bit6 = bottom row, bit7 unused). LCD_DrawChar tests (1U << gr).     */
/*====================================================================*/
static const uint8_t s_font5x7[96][5] = {
    {0x00,0x00,0x00,0x00,0x00},{0x00,0x00,0x5F,0x00,0x00},
    {0x00,0x07,0x00,0x07,0x00},{0x14,0x7F,0x14,0x7F,0x14},
    {0x24,0x2A,0x7F,0x2A,0x12},{0x23,0x13,0x08,0x64,0x62},
    {0x36,0x49,0x55,0x22,0x50},{0x00,0x05,0x03,0x00,0x00},
    {0x00,0x1C,0x22,0x41,0x00},{0x00,0x41,0x22,0x1C,0x00},
    {0x08,0x2A,0x1C,0x2A,0x08},{0x08,0x08,0x3E,0x08,0x08},
    {0x00,0x50,0x30,0x00,0x00},{0x08,0x08,0x08,0x08,0x08},
    {0x00,0x60,0x60,0x00,0x00},{0x20,0x10,0x08,0x04,0x02},
    {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},
    {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4B,0x31},
    {0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},
    {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},
    {0x36,0x49,0x49,0x49,0x36},{0x06,0x49,0x49,0x29,0x1E},
    {0x00,0x36,0x36,0x00,0x00},{0x00,0x56,0x36,0x00,0x00},
    {0x00,0x08,0x14,0x22,0x41},{0x14,0x14,0x14,0x14,0x14},
    {0x41,0x22,0x14,0x08,0x00},{0x02,0x01,0x51,0x09,0x06},
    {0x32,0x49,0x79,0x41,0x3E},{0x7E,0x11,0x11,0x11,0x7E},
    {0x7F,0x49,0x49,0x49,0x36},{0x3E,0x41,0x41,0x41,0x22},
    {0x7F,0x41,0x41,0x22,0x1C},{0x7F,0x49,0x49,0x49,0x41},
    {0x7F,0x09,0x09,0x01,0x01},{0x3E,0x41,0x41,0x51,0x32},
    {0x7F,0x08,0x08,0x08,0x7F},{0x00,0x41,0x7F,0x41,0x00},
    {0x20,0x40,0x41,0x3F,0x01},{0x7F,0x08,0x14,0x22,0x41},
    {0x7F,0x40,0x40,0x40,0x40},{0x7F,0x02,0x04,0x02,0x7F},
    {0x7F,0x04,0x08,0x10,0x7F},{0x3E,0x41,0x41,0x41,0x3E},
    {0x7F,0x09,0x09,0x09,0x06},{0x3E,0x41,0x51,0x21,0x5E},
    {0x7F,0x09,0x19,0x29,0x46},{0x46,0x49,0x49,0x49,0x31},
    {0x01,0x01,0x7F,0x01,0x01},{0x3F,0x40,0x40,0x40,0x3F},
    {0x1F,0x20,0x40,0x20,0x1F},{0x3F,0x40,0x38,0x40,0x3F},
    {0x63,0x14,0x08,0x14,0x63},{0x03,0x04,0x78,0x04,0x03},
    {0x61,0x51,0x49,0x45,0x43},{0x00,0x00,0x7F,0x41,0x41},
    {0x02,0x04,0x08,0x10,0x20},{0x41,0x41,0x7F,0x00,0x00},
    {0x04,0x02,0x01,0x02,0x04},{0x40,0x40,0x40,0x40,0x40},
    {0x00,0x01,0x02,0x04,0x00},{0x20,0x54,0x54,0x54,0x78},
    {0x7F,0x48,0x44,0x44,0x38},{0x38,0x44,0x44,0x44,0x20},
    {0x38,0x44,0x44,0x48,0x7F},{0x38,0x54,0x54,0x54,0x18},
    {0x08,0x7E,0x09,0x01,0x02},{0x0C,0x52,0x52,0x52,0x3E},
    {0x7F,0x08,0x04,0x04,0x78},{0x00,0x44,0x7D,0x40,0x00},
    {0x20,0x40,0x44,0x3D,0x00},{0x7F,0x10,0x28,0x44,0x00},
    {0x00,0x41,0x7F,0x40,0x00},{0x7C,0x04,0x18,0x04,0x78},
    {0x7C,0x08,0x04,0x04,0x78},{0x38,0x44,0x44,0x44,0x38},
    {0x7C,0x14,0x14,0x14,0x08},{0x08,0x14,0x14,0x18,0x7C},
    {0x7C,0x08,0x04,0x04,0x08},{0x48,0x54,0x54,0x54,0x20},
    {0x04,0x3F,0x44,0x40,0x20},{0x3C,0x40,0x40,0x20,0x7C},
    {0x1C,0x20,0x40,0x20,0x1C},{0x3C,0x40,0x30,0x40,0x3C},
    {0x44,0x28,0x10,0x28,0x44},{0x0C,0x50,0x50,0x50,0x3C},
    {0x44,0x64,0x54,0x4C,0x44},{0x00,0x08,0x36,0x41,0x00},
    {0x00,0x00,0x7F,0x00,0x00},{0x00,0x41,0x36,0x08,0x00},
    {0x08,0x04,0x08,0x10,0x08},{0xFF,0xFF,0xFF,0xFF,0xFF}
};

/* Draw one character in a 12x16 cell: 5x7 glyph scaled x2 (10x14)
   plus 2px right/bottom spacing. Pixel loop: 12x16 = 192 pixels. */
static void LCD_DrawChar(uint16_t x, uint16_t y, char ch,
                         uint16_t fg, uint16_t bg)
{
    const uint8_t *glyph;
    uint16_t cy;    /* cell row    */
    uint16_t cx;    /* cell column */

    if ((ch < 0x20) || (ch > 0x7E))
    {
        ch = ' ';
    }
    glyph = s_font5x7[ch - 0x20];

    LCD_CS_LOW();
    LCD_SetWindow(x, y, (uint16_t)(x + LCD_CHAR_CELL_W - 1U),
                  (uint16_t)(y + LCD_CHAR_CELL_H - 1U));

    for (cy = 0U; cy < LCD_CHAR_CELL_H; cy++)
    {
        /* glyph row: 7 rows x2 = 14, rows 14..15 stay background */
        uint16_t gr = cy / LCD_FONT_SCALE;
        uint16_t in_glyph_row = (cy < (7U * LCD_FONT_SCALE)) ? 1U : 0U;

        for (cx = 0U; cx < LCD_CHAR_CELL_W; cx++)
        {
            uint16_t px;
            uint16_t gc = cx / LCD_FONT_SCALE;
            uint16_t in_glyph_col = (cx < (5U * LCD_FONT_SCALE)) ? 1U : 0U;

            if (in_glyph_row && in_glyph_col &&
                ((glyph[gc] & (0x01U << gr)) != 0U))
            {
                px = fg;
            }
            else
            {
                px = bg;
            }
            LCD_PushPixel((uint16_t)px);
        }
    }
    LCD_CS_HIGH();
}

void LCD_Print(uint8_t row, uint8_t col, const char *text,
               uint16_t fg, uint16_t bg)
{
    uint16_t x = (uint16_t)col * LCD_CHAR_CELL_W;
    uint16_t y = (uint16_t)row * LCD_CHAR_CELL_H;

    if (row >= LCD_CHAR_ROWS)
    {
        return;
    }
    while (*text != '\0')
    {
        if (col >= LCD_CHAR_COLS)
        {
            break;
        }
        LCD_DrawChar(x, y, *text, fg, bg);
        x += LCD_CHAR_CELL_W;
        col++;
        text++;
    }
}

void LCD_PrintField(uint8_t row, uint8_t col, const char *text,
                    uint8_t width, uint16_t fg, uint16_t bg)
{
    char buf[40];
    uint8_t i;
    uint8_t len;

    if (width > (uint8_t)sizeof(buf))
    {
        width = (uint8_t)sizeof(buf);
    }
    /* measure text length (bounded) */
    len = 0U;
    while ((text[len] != '\0') && (len < width))
    {
        len++;
    }
    for (i = 0U; i < width; i++)
    {
        buf[i] = (i < len) ? text[i] : ' ';
    }
    buf[width] = '\0';
    LCD_Print(row, col, buf, fg, bg);
}
