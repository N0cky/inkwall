#pragma once
#include <Arduino.h>
#include <SPI.h>
#include "config.h"

// ════════════════════════════════════════════════════════════════════════════
//  Waveshare 13.3″ Spectra 6 (EPD_13IN3E) – SPI-Treiber
//
//  Das Display ist intern in zwei Hälften aufgeteilt:
//    Master CS (CS_M) → linke 600 Pixel  jeder Zeile
//    Slave  CS (CS_S) → rechte 600 Pixel jeder Zeile
//
//  Datenformat: 4 Bit pro Pixel, 2 Pixel pro Byte
//    Byte = (linkes_Pixel << 4) | rechtes_Pixel
//
//  BUSY-Pin: LOW = beschäftigt, HIGH = bereit
//
//  Farb-Codes:
//    0x0 Schwarz  0x1 Weiß  0x2 Gelb  0x3 Rot  0x5 Blau  0x6 Grün
// ════════════════════════════════════════════════════════════════════════════

#define EPD_COLOR_BLACK   0x0
#define EPD_COLOR_WHITE   0x1
#define EPD_COLOR_YELLOW  0x2
#define EPD_COLOR_RED     0x3
// 0x4 nicht verwendet
#define EPD_COLOR_BLUE    0x5
#define EPD_COLOR_GREEN   0x6

// EPD_WIDTH / 2 Bytes pro Zeile (4bpp), davon je die Hälfte pro CS
#define EPD_ROW_BYTES      (EPD_WIDTH / 2)        // 600
#define EPD_HALF_ROW_BYTES (EPD_ROW_BYTES / 2)    // 300

void EPD_Init(void);
// false: das Panel hat nicht geantwortet (BUSY blieb aktiv) – das Bild steht dann nicht sicher da
bool EPD_Display(const uint8_t* imageBuffer);   // imageBuffer: EPD_ROW_BYTES × EPD_HEIGHT Bytes
bool EPD_Clear(uint8_t color);
void EPD_Sleep(void);
