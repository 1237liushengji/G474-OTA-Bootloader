/**
  ******************************************************************************
  * @file    bl_image.c
  * @brief   镜像头解析与跳转前校验实现
  *
  * 镜像布局（契约 ota_common.h）：
  *   槽基址 + 0x00 : 32B 镜像头（magic "G4OT" / imgSize / payloadSize / version /
  *                   loadAddr / payloadCrc / hdrCrc / reserved，全部小端）
  *   槽基址 + 0x80 : APP 链接基址，净固件起始（loadAddr 必须等于该值）
  *                   —— 保留区是 OTA_IMG_HDR_SIZE = 0x80（不是 32！），
  *                      因为 Cortex-M4 的 VTOR[6:0] 是 RES0，**向量表必须 0x80 对齐**；
  *                      曾经用 32 → APP 基址 0x0800C020 → VTOR 被吞成 0x0800C000
  *                      → 向量表落到镜像头上 → INVSTATE/IBUSERR → lockup（见 docs/11）。
  *
  * 完整性三层（与 docs/03 §9 一致）：
  *   1) 头部自校验：hdrCrc == CRC32(头[0,24))
  *   2) 净固件端到端：payloadCrc == CRC32(净固件[0, payloadSize))
  *   3) **独立**结构校验：栈顶指针（必须在 IRAM 内且 8B 对齐）与复位向量
  *      （bit0=1 且落在本槽净荷范围内）——挡"CRC 正确但向量表被破坏"的极端场景
  ******************************************************************************
  */
#include "bl_image.h"
#include "bl_crc32.h"
#include <string.h>

/*--------------------------------------- 内部工具 ----------------------------------------*/
/**
  * @brief  从小端字节流读 u32（镜像头字段全部小端）
  */
static uint32_t BL_ImageRdU32(const uint8_t *p)
{
  return ((uint32_t)p[0]) | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/**
  * @brief  向小端字节流写 u32
  */
static void BL_ImageWrU32(uint8_t *p, uint32_t v)
{
  p[0] = (uint8_t)(v & 0xFFU);
  p[1] = (uint8_t)((v >> 8) & 0xFFU);
  p[2] = (uint8_t)((v >> 16) & 0xFFU);
  p[3] = (uint8_t)((v >> 24) & 0xFFU);
}

/*--------------------------------------- 头解析 ------------------------------------------*/
/**
  * @brief  在槽首最多 OTA_IMG_HDR_SEARCH_N 个 256B 对齐位置搜索镜像头并校验头部字段
  */
BL_ImgResult BL_ImageReadHeader(uint8_t slot, BL_ImageInfo *info)
{
  uint32_t       slotBase;
  uint32_t       hdrAddr = 0U;
  uint32_t       payloadSize;
  uint32_t       imgSize;
  uint32_t       loadAddr;
  const uint8_t *p;
  uint32_t       i;

  if (slot > 1U)
  {
    return BL_IMG_ERR_SIZE_RANGE;
  }

  slotBase = (slot == 0U) ? OTA_SLOT_A_ADDR : OTA_SLOT_B_ADDR;

  /* 【M1 修复 / 血线 11】槽内只查槽首一处。
   * 原实现按 OTA_IMG_HDR_SEARCH_N=2 搜 slotBase 与 slotBase+0x100，而 +0x100
   * 已落在**净荷区**（appBase = slotBase + 0x80）内部，后果有二：
   *   1) 净荷里任何形似 "G4OT" 的 4 字节会被提升为合法镜像头（哪怕 hdrCrc 也过）；
   *   2) 下面的 loadAddr 校验锚在 hdrAddr，使"载入地址 = 本槽 APP 基址"被静默放宽，
   *      且 CRC 会越界多读 0x100 字节 —— Slot A 下直接跨过分区边界读进 MIRROR 控制页。
   * 契约已把 OTA_IMG_HDR_SEARCH_N 锁死为 1 并加了编译期 #error，此处保留循环结构
   * 只为契约仍是单一真相源；若未来契约真的放开，行为会自动跟随。 */
  for (i = 0U; i < OTA_IMG_HDR_SEARCH_N; i++)
  {
    uint32_t a = slotBase + (i * OTA_IMG_HDR_SEARCH_STEP);

    if (BL_ImageRdU32((const uint8_t *)a) == OTA_IMG_MAGIC)
    {
      hdrAddr = a;
      break;
    }
  }
  if (hdrAddr == 0U)
  {
    return BL_IMG_ERR_HDR_MAGIC;
  }

  p = (const uint8_t *)hdrAddr;

  /* 头部自校验：hdrCrc == CRC32(头[0,24)) */
  if (BL_ImageRdU32(p + OTA_IMG_OFF_HDR_CRC) != BL_Crc32(p, (uint32_t)OTA_IMG_OFF_HDR_CRC))
  {
    return BL_IMG_ERR_HDR_CRC;
  }

  payloadSize = BL_ImageRdU32(p + OTA_IMG_OFF_PAYLOAD_SIZE);
  imgSize     = BL_ImageRdU32(p + OTA_IMG_OFF_IMG_SIZE);
  loadAddr    = BL_ImageRdU32(p + OTA_IMG_OFF_LOAD_ADDR);

  if ((payloadSize == 0U) || (payloadSize > OTA_IMG_MAX_PAYLOAD))
  {
    return BL_IMG_ERR_SIZE_RANGE;
  }
  if (imgSize != (payloadSize + OTA_IMG_HDR_SIZE))
  {
    return BL_IMG_ERR_SIZE_RANGE;
  }
  if (loadAddr != (slotBase + OTA_IMG_HDR_SIZE))
  {
    /* 【M1 修复】锚点从 hdrAddr 改为 **slotBase**。
     * 契约要求"载入地址必须等于本槽 APP 链接基址"，而该基址是
     * slotBase + OTA_IMG_HDR_SIZE，与头实际被找到的位置**无关**。
     * 用 hdrAddr 做锚点等于把契约放宽成"等于头位置+0x80"—— 头一旦被挪到别处，
     * 校验锚点也跟着漂移，形同虚设（这正是原实现的隐患）。 */
    return BL_IMG_ERR_LOAD_ADDR;
  }

  /* 【M2 修复】显式校验"净荷不越槽尾"。
   * 上面`payloadSize > OTA_IMG_MAX_PAYLOAD` 只在 hdrAddr == slotBase 时才等价于
   * 越界检查（OTA_IMG_MAX_PAYLOAD 是按 slotBase 定义的）；一旦头位置漂移该等式
   * 就不成立。补一条显式的、与锚点无关的检查形成双保险。
   * 写成减法形式：左边的减法在 payloadSize > OTA_SLOT_SIZE 时已先被排除，
   * 不会因无符号下溢而误判为"通过"。 */
  if ((payloadSize > OTA_SLOT_SIZE) ||
      (payloadSize > (OTA_SLOT_SIZE - OTA_IMG_HDR_SIZE)))
  {
    return BL_IMG_ERR_SIZE_RANGE;
  }

  if (info != NULL)
  {
    info->slot        = slot;
    info->hdrAddr     = hdrAddr;
    info->appBase     = slotBase + OTA_IMG_HDR_SIZE;   /* 【M1】锚定槽首，不随 hdrAddr 漂移 */
    info->payloadSize = payloadSize;
    info->imgSize     = imgSize;
    info->loadAddr    = loadAddr;
    info->payloadCrc  = BL_ImageRdU32(p + OTA_IMG_OFF_PAYLOAD_CRC);
    info->major       = p[OTA_IMG_OFF_VERSION];
    info->minor       = p[OTA_IMG_OFF_VERSION + 1U];
    info->version     = ((uint32_t)info->major << 24) | ((uint32_t)info->minor << 16);
  }

  return BL_IMG_OK;
}

/*--------------------------------------- 完整校验 ----------------------------------------*/
/**
  * @brief  完整跳转前校验（含两个独立的结构检查）
  */
BL_ImgResult BL_ImageValidate(uint8_t slot, BL_ImageInfo *info)
{
  BL_ImageInfo local;
  BL_ImgResult result;
  const uint8_t *p;
  uint32_t     sp;
  uint32_t     rv;

  (void)memset(&local, 0, sizeof(local));

  result = BL_ImageReadHeader(slot, &local);
  if (result != BL_IMG_OK)
  {
    return result;
  }

  p = (const uint8_t *)local.appBase;

  /* 净固件 CRC32：端到端完整性（唯一位于 CAN CRC 之外的一层） */
  if (local.payloadCrc != BL_Crc32(p, local.payloadSize))
  {
    return BL_IMG_ERR_PAYLOAD_CRC;
  }

  /* 独立检查 1：栈顶指针必须落在内核 RAM、且 8 字节对齐
   * （允许 one-past-end 的 BL_IRAM_TOP：ARM 惯例初始 SP = RAM 顶端）
   *
   * 【M3 修复】下界由 `sp < BL_IRAM_BASE` 改为 `sp <= BL_IRAM_BASE`。
   *   sp == 0x20000000 恰好是 RAM 首地址，字面上"落在 IRAM 内"，但它的含义是
   *   "栈从 RAM 的第一个字节往下长" —— 即第一次压栈就写到 0x1FFFFFF，
   *   **必然 HardFault**。这恰恰是这条检查最该拦住的典型坏值，原写法却放过了它。
   *   注意与上界那条区分：上界用 `sp > BL_IRAM_END` 是**正确**的，因为
   *   BL_IRAM_TOP 是合法的 one-past-end（初值 SP 就等于 RAM 顶端）。 */
  sp = BL_ImageRdU32(p);
  if (((sp % 8U) != 0U) || (sp <= BL_IRAM_BASE) ||
      ((sp > BL_IRAM_END) && (sp != BL_IRAM_TOP)))
  {
    return BL_IMG_ERR_SP;
  }

  /* 独立检查2：复位向量 bit0=1（Thumb 状态）且必须落在本槽净荷范围内 */
  rv = BL_ImageRdU32(p + 4U);
  if (((rv & 0x1U) == 0U) || (rv < local.appBase) || (rv >= (local.appBase + local.payloadSize)))
  {
    return BL_IMG_ERR_RESET_VEC;
  }

  if (info != NULL)
  {
    *info = local;
  }

  return BL_IMG_OK;
}

/**
  * @brief  校验结果 -> OTA_ERR_xxx
  */
uint32_t BL_ImageErrToOtaErr(BL_ImgResult result)
{
  switch (result)
  {
    case BL_IMG_ERR_HDR_MAGIC:   return OTA_ERR_HDR_MAGIC;
    case BL_IMG_ERR_HDR_CRC:     return OTA_ERR_HDR_CRC;
    case BL_IMG_ERR_PAYLOAD_CRC: return OTA_ERR_PAYLOAD_CRC;
    case BL_IMG_ERR_SIZE_RANGE:  return OTA_ERR_SIZE_RANGE;
    case BL_IMG_ERR_LOAD_ADDR:   return OTA_ERR_LOAD_ADDR;
    case BL_IMG_ERR_SP:          return OTA_ERR_SP;
    case BL_IMG_ERR_RESET_VEC:   return OTA_ERR_RESET_VEC;
    case BL_IMG_OK:
    default:                     return OTA_ERR_NONE;
  }
}

/**
  * @brief  合成 32B 镜像头（模式A：设备侧写头）
  */
void BL_ImageBuildHeader(uint8_t *hdr, uint32_t payloadSize, uint32_t loadAddr,
                         uint32_t payloadCrc, uint8_t major, uint8_t minor)
{
  uint32_t i;

  if (hdr == NULL)
  {
    return;
  }

  for (i = 0U; i < OTA_IMG_HDR_SIZE; i++)
  {
    hdr[i] = 0U;                                   /* 保留字段一并清零，保证 hdrCrc 可复现 */
  }

  BL_ImageWrU32(&hdr[OTA_IMG_OFF_MAGIC], OTA_IMG_MAGIC);
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_IMG_SIZE], payloadSize + OTA_IMG_HDR_SIZE);
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_PAYLOAD_SIZE], payloadSize);
  hdr[OTA_IMG_OFF_VERSION]      = major;
  hdr[OTA_IMG_OFF_VERSION + 1U] = minor;
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_LOAD_ADDR], loadAddr);
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_PAYLOAD_CRC], payloadCrc);
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_RESERVED], 0U);

  /* hdrCrc 最后算：覆盖 [0,24)，本身位于偏移 24 之外，不受影响 */
  BL_ImageWrU32(&hdr[OTA_IMG_OFF_HDR_CRC], BL_Crc32(hdr, (uint32_t)OTA_IMG_OFF_HDR_CRC));
}
