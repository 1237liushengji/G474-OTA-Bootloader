/**
  ******************************************************************************
  * @file    ota_common.h
  * @brief   OTA/Bootloader 全局契约：Flash 分区、镜像格式、UDS 服务与例程、配置区结构
  *
  * 本文件是 Bootloader 工程、APP 工程、PC 上位机三方共用的唯一真相源（Single Source of Truth）。
  * 任何地址/魔数/服务号的改动必须先改本文件，再同步 ota/tools/ota_host.py 内的对应常量。
  *
  * 目标器件：STM32G474VET6
  *   Flash 512KB @ 0x08000000~0x0807FFFF，页大小 2KB（G4 全系列页粒度均为 2KB）
  *   RAM 128KB  @ 0x20000000~0x2001FFFF
  *
  * 分区策略说明（重要）：
  *   G474VE 的 512KB Flash 可经选项字节 DBANK=1 配置为双 Bank（各 256KB）：
  *     Bank1 = 0x08000000~0x0803FFFF，Bank2 = 0x08040000~0x0807FFFF
  *   本方案把两个 APP 槽位刻意对齐到两个物理 Bank 边界，从而获得"擦写 B 槽所在的 Bank2
  *   时仍可从 Bank1 取指执行"的读-写并行（RWW）能力：
  *     Slot A 完全落在 Bank1；Slot B 完全落在 Bank2。
  *   注意：DBANK 是全局选项字节，B 槽的取指若跨越 Bank2 上界会 HardFault，
  *   因此 Slot B 必须"下对齐到 Bank2 起始"，而不是"上对齐到 Flash 末尾"。
  ******************************************************************************
  */
#ifndef __OTA_COMMON_H
#define __OTA_COMMON_H

#include <stdint.h>
#include <stddef.h>   /* offsetof：结构布局断言用 */

/*===================================== Flash 基本参数 =====================================*/
#define OTA_FLASH_BASE            0x08000000U   /* Flash 映射基址 */
#define OTA_FLASH_SIZE            0x00080000U   /* 512 KB */
#define OTA_FLASH_END             (OTA_FLASH_BASE + OTA_FLASH_SIZE - 1U)
#define OTA_FLASH_PAGE_SIZE       0x00000800U   /* 2 KB，G4 全系列统一页粒度 */
#define OTA_FLASH_BANK_SIZE       0x00040000U   /* DBANK=1 时每 Bank 256 KB */

/*===================================== 分区表 ============================================*/
/* 地址        大小    名称            用途
 * 0x08000000  2 KB    BL_HEADER       BL 向量表(页首 ~0x1D8 字节) + 控制区主份两份副本
 * 0x08000800  46 KB   BL_CODE         Bootloader 代码（向量表必须在 0x08000000，代码紧随其后）
 * 0x0800C000  208 KB  SLOT_A          APP 槽 A（含 0x80 槽首保留区：32B 头 + 填充），恰好抵 Bank1 上界
 * 0x08040000  2 KB    BL_HEADER_MIR   控制区镜像份（更新 Slot B 时本页随 Bank2 被擦，故需镜像）
 * 0x08040800  208 KB  SLOT_B          APP 槽 B（含 0x80 槽首保留区），落在 Bank2
 *
 * 关键约束（为什么两槽同尺寸）：
 *   Slot A 必须**上不越 Bank1 上界 0x08040000**（否则取指跨越 Bank 边界 → DBANK 下 HardFault）；
 *   Slot B 必须**下对齐 Bank2 起始 0x08040000**（同上）。
 *   Slot A 起点由 BL 区大小决定（BL 46KB -> Slot A 从 0x0800C000 起），
*   两槽共用同一个 SIZE 常量（208KB），因此仍只需一个 OTA_SLOT_SIZE。
 *   两槽都满足约束：
 *     Slot A: [0x0800C000, 0x08040000)  ⊂ Bank1
 *     Slot B: [0x08040800, 0x08078800)  ⊂ Bank2
 *
 * ⚠️ BL 代码区与向量表/控制区的关系（本方案的血线）：
 *   - BL 向量表**必须**位于 0x08000000（MCU 复位固定从该地址取初始 SP 与复位向量）；
 *   - BL 代码从 0x08000800 起，到 0x0800B800 为止（46KB 预算；实测固件 32752 字节）；
 *   - 控制区主份(两份副本)与向量表**同页**：向量表只占页首 ~0x1D8 字节，
 *     两份副本分别位于 +0x000 与 +0x800，互不重叠，余量充足。
 *   - 因此 `addr < OTA_SLOT_A_ADDR`（= 0x0800C000）的保护判断恰好同时护住
 *     向量表页 + 控制区主份 + **整个 BL 代码区**。这是保护逻辑与分区表对齐的设计意图，
 *     不要在 Slot A 之前插入任何可写数据区。                                        */
#define OTA_CFG_PRIMARY_ADDR      0x08000000U
#define OTA_CFG_MIRROR_ADDR       0x08040000U
#define OTA_CFG_PAGE_SIZE         OTA_FLASH_PAGE_SIZE          /* 2 KB */
#define OTA_CFG_VECTAB_BYTES      0x000001D8U                  /* BL 向量表占用的页首字节数（G4 中断表实测） */
/* 页内两份副本的偏移（**都必须在同一页内**，否则会落到下一页的 BL 代码区/Slot B 首部）：
 *   +0x000 : BL 向量表（~0x1D8 字节）—— 绝不能作为配置写目标
 *   +0x1D8 : 副本0（紧接向量表之后）
 *   +0x400 : 副本1（页内后半）
 * 两个偏移都满足 8 字节对齐（Flash 双字编程要求）。 */
#define OTA_CFG_SLOT_OFFSET       OTA_CFG_VECTAB_BYTES         /* 0x1D8，向量表之后 */
#define OTA_CFG_SLOT_MIRROR_OFF   0x00000400U                  /* 0x400，页内后半（**不是 0x800**） */

#define OTA_BL_CODE_ADDR          0x08000800U
#define OTA_BL_CODE_SIZE          0x0000B000U   /* 46 KB（0x08000800~0x0800B7FF）
                                                 * 实测 BL 固件 32752 字节（32KB），30KB 不够；
                                                 * 预留 46KB 留出增长余量与调试构建空间。 */

#define OTA_SLOT_A_ADDR           0x0800C000U   /* = BL 区结束后下一页对齐 */
#define OTA_SLOT_B_ADDR           0x08040800U
#define OTA_SLOT_SIZE             0x00034000U   /* 208 KB = (Bank1 上界 - Slot A 起点)；
                                                 * 因 BL 扩到 46KB，Slot A 相应缩小，
                                                 * 但仍**恰好抵 Bank1 上界** 0x08040000。 */

/* 槽内镜像头的检索位置 —— **只有槽首一处**（2026-10-07 修正，原为 2 处）。
 *
 * ⚠️ 为什么不能"往后多找几处"（血线11）：
 *   契约规定镜像头**恒在 slotBase+0**，净固件恒在 slotBase+0x80。若允许搜索
 *   slotBase+0x100，该位置已落在**净荷区**内部，于是有两个严重后果：
 *     1) 净荷里任何形似 "G4OT" 的 4 字节数据会被**提升为合法镜像头**
 *        （哪怕 hdrCrc 恰好也通过），等于凭空造出一个可跳转的"合法镜像"；
 *     2) `loadAddr` 校验锚在 `hdrAddr` 而非 `slotBase`，于是"载入地址必须等于
 *        本槽 APP 基址"这条契约被**静默放宽**；若 hdrAddr = +0x100，
 *        CRC 计算还会越界多读 0x100 字节 —— Slot A 情况下直接跨过分区边界
 *        读进 MIRROR 控制页。
 *   搜索"更后面的位置"没有任何正向用途：BL_ImageBuildHeader 恒在槽首写头，
 *   上位机 --pack 也在 .bin 前拼接到槽首。
 *
 * 保留这两个宏是为了不破坏既有引用，但 N 固定为 1 —— 即"只查槽首"。 */
#define OTA_IMG_HDR_SEARCH_N      1U
#define OTA_IMG_HDR_SEARCH_STEP   0x00000100U

/*===================================== 镜像头格式 ========================================*/
/* 上位机在 MDK 生成的 .bin 前拼接 32 字节的头**结构体**（OTA_IMG_HDR_STRUCT_SIZE），
 * 槽首保留区则是 0x80（OTA_IMG_HDR_SIZE，含[32,0x80) 填充），地址字段填"槽内绝对地址"。
 * APP 链接基址 = 槽基址 + OTA_IMG_HDR_SIZE（例如 Slot A 工程 IROM1 起始 = 0x0800C080）。
 *
 * ⚠️ 血线12（2026-10-07 补，L1）：**头结构体 32B≠ 槽首保留区 0x80**
 *   本项目曾因"32 vs 0x80"变砖过一次（VTOR 必须 0x80 对齐，见下）。
 *   两个数字都正确，但各管一件事：
 *     - OTA_IMG_HDR_STRUCT_SIZE = 32   -> 头里有哪些字段、CRC 覆盖 [0,24)
 *     - OTA_IMG_HDR_SIZE         = 0x80 -> **占多少地址空间**，决定 APP 基址
 *   凡涉及"APP 基址 = 槽基址 + N"的表述，N 一律是 **0x80**，绝不能写 32。
 *   头文件里复述旧数字（"+32B 镜像头"）极易误导后来者重新踩坑，故全文清理。
 *
 * ⚠️ 为什么保留区是 **0x80** 而不是 32 —— 这是用板子换来的教训（见 docs/11）：
 *   Cortex-M4 的 `SCB->VTOR` 位 [6:0] 是 **RES0**（保留、读作 0，写入被忽略），
 *   即**向量表必须按 0x80 字节对齐**。而 APP 的链接基址 = 槽基址 + 保留区：
 *     槽基址 0x0800C000 是页对齐的，只要保留区是 32，APP 基址就是 0x0800C020 ——
 *     **写进 VTOR 只会存成 0x0800C000**，向量表落到 32 字节镜像头上。
 *   后果：APP 一开 SysTick 就取到垃圾向量 -> INVSTATE/IBUSERR -> **lockup**
 *   （实测 VTOR=0x0800C000、CFSR=0x00020100、HFSR=0x40000000；现象是"上电没反应"）。
 *   把保留区设为 0x80 后，APP 基址 = 0x0800C080 / 0x08040880，天然 0x80 对齐。
 *
 *   头结构体本身仍只占前 32 字节；[32, 0x80) 是填充区（写 0 或保持 0xFF 都行，
 *   头部校验只覆盖 [0,24) 与 hdrCrc，不涉及填充区）。 */
#define OTA_IMG_MAGIC             0x47344F54U   /* "G4OT"（小端字节序列 47 34 4F 54） */
#define OTA_IMG_HDR_SIZE          0x00000080U   /* 槽首保留区 = 头 + 填充，**必须 0x80 对齐** */
#define OTA_IMG_HDR_STRUCT_SIZE   32U           /* 头结构体实际占用（magic..hdrCrc+reserved） */

/* 血线 11 守卫：搜索范围绝不能伸进净荷区。
 * STEP 是 0x100 > HDR_SIZE(0x80) —— 只要 N >= 2，第二次搜索就必然落在
 * [slotBase+0x80, slotBase+0x100) 这段净荷里。这里锁死，任何人调大 N 都会在编译期
 * 被拦下，而不是等到"某个镜像的净荷里碰巧有 G4OT"才现场暴露。
 *
 * ⚠️ 两条踩过的坑（2026-10-09 首次真编译才暴露，静态守卫完全测不到）：
 *  1) 判定必须用 **(N-1)*STEP** 而非 N*STEP：搜索是 for (i=0; i<N; i++) 查
 *     slotBase + i*STEP，i 从 0 起，N=1 时唯一位置是 slotBase+0（就在头内、不越界）。
 *     写成 N*STEP 会算得 0x100 > 0x80 而误报，把 11 个 .c 全部编译不下去。
 *  2) 本守卫**必须**放在 OTA_IMG_HDR_SIZE 定义之后。写在它前面时该宏尚未定义，
 *     预处理器把 OTA_IMG_HDR_SIZE 当普通标识符，同样导致全量编译失败。
 */
#if (OTA_IMG_HDR_SEARCH_N > 1U)
#error "OTA_IMG_HDR_SEARCH_N 必须为 1：镜像头恒在槽首 slotBase+0，搜索第2 个位置会伸进净荷区（见血线 11）"
#endif
#if (((OTA_IMG_HDR_SEARCH_N - 1U) * OTA_IMG_HDR_SEARCH_STEP) >= OTA_IMG_HDR_SIZE)
#error "镜像头最后一个搜索位置已越出 OTA_IMG_HDR_SIZE(0x80)，会搜进净荷区"
#endif

/* 镜像头字段偏移（全部小端） */
#define OTA_IMG_OFF_MAGIC         0U    /* u32 魔数（小端存储：落盘字节 54 4F 34 47） */
#define OTA_IMG_OFF_IMG_SIZE      4U    /* u32 整镜像大小 = **OTA_IMG_HDR_SIZE(0x80) + payloadSize**
                                         * ⚠️ 不是 "32 + payloadSize"！曾因这句注释误导，
                                         *    APP 合成头时写成 32+payload，比 BL 要求小 0x60，
                                         *    导致 FF02 成功但复位后 BL 判 SIZE_RANGE(4) 回退旧槽。 */
#define OTA_IMG_OFF_PAYLOAD_SIZE  8U    /* u32 净固件字节数 */
#define OTA_IMG_OFF_VERSION       12U   /* u8 major, u8 minor, u16 保留 */
#define OTA_IMG_OFF_LOAD_ADDR     16U   /* u32 净固件应写入的绝对地址 */
#define OTA_IMG_OFF_PAYLOAD_CRC   20U   /* u32 净固件 CRC32 */
#define OTA_IMG_OFF_HDR_CRC       24U   /* u32 头部校验：对 [0,24) 计算 CRC32 */
#define OTA_IMG_OFF_RESERVED      28U   /* u32 保留，写 0 */

#define OTA_IMG_MAX_PAYLOAD       (OTA_SLOT_SIZE - OTA_IMG_HDR_SIZE)

/*===================================== CRC32 =============================================*/
/* 采用标准 CRC-32/ISO-HDLC（与 zlib.crc32 完全一致）：
 *   多项式 0x04C11DB7 反射写为 0xEDB88320；初值 0xFFFFFFFF；结果异或 0xFFFFFFFF。
 *   自检向量：CRC32("123456789") == 0xCBF43926
 * 软件实现而非用 G4 硬件 CRC 外设，原因：本工程 CubeMX 未使能 CRC 外设，
 * 且 Bootloader/APP/上位机三方必须用完全一致的算法，纯软件实现最易对齐。 */
#define OTA_CRC32_CHECK_VECTOR    0xCBF43926UL
#define OTA_CRC32_CHECK_STR       "123456789"

/*===================================== UDS 服务与例程 ===================================*/
/* 复用工程既有的 ISO-TP 物理寻址（isotp.h）：
 *   请求 0x7E0（诊断仪 -> 目标节点），应答 0x7E8（目标节点 -> 诊断仪）
 * 服务集在既有 0x10/0x22/0x2E/0x19/0x3E 基础上新增： */
#define UDS_SID_DIAG_SESSION      0x10U
#define UDS_SID_ECU_RESET         0x11U
#define UDS_SID_READ_DID          0x22U
#define UDS_SID_WRITE_DID         0x2EU
#define UDS_SID_ROUTINE_CTRL      0x31U
#define UDS_SID_REQUEST_DOWNLOAD  0x34U
#define UDS_SID_TRANSFER_DATA     0x36U
#define UDS_SID_TRANSFER_EXIT     0x37U
#define UDS_SID_TESTER_PRESENT    0x3EU

/* RoutineControl 子功能与例程标识 */
/* 子功能字节的 bit7 是 suppressPosRspMsg（ISO 14229-1）：
 * 置 1 表示"成功就别回肯定响应"。比较子功能号前必须先屏蔽该位，
 * 否则 `31 81 01 FF01` 这类请求会被判为不支持的子功能（NRC 0x12）。 */
#define UDS_SUPPRESS_POS_RSP      0x80U
#define UDS_SUBFUNC_MASK          0x7FU   /* = ~UDS_SUPPRESS_POS_RSP，取子功能号用 */
#define UDS_RC_START              0x01U
#define UDS_RC_STOP               0x02U
#define UDS_RC_RESULT             0x03U

#define UDS_RID_ERASE             0xFF00U   /* 擦除目标槽（0x34 之前的显式准备；0x34 亦会隐式擦除） */
#define UDS_RID_VERIFY            0xFF01U   /* 擦写完成后按 CRC32 校验目标槽 */
#define UDS_RID_ACTIVATE          0xFF02U   /* 提交：置 TRIAL_PENDING 并复位，由上层判定是否调用 */
#define UDS_RID_ROLLBACK          0xFF03U   /* 显式回滚：切回另一个槽并清 pending */
#define UDS_RID_COMMIT            0xFF04U   /* 试运行确认：TRIAL -> IDLE，正式生效（APP 自检通过后调用） */

/* ReadDataByIdentifier：OTA 相关 DID */
#define UDS_DID_OTA_STATE         0xF180U   /* 1 字节 OTA 状态 */
#define UDS_DID_ACTIVE_SLOT       0xF181U   /* 1 字节 0=A 1=B */
#define UDS_DID_OTA_VERSION       0xF182U   /* 4 字节：已安装镜像 major/minor + 另一槽版本 */
#define UDS_DID_SLOT_CRC          0xF183U   /* 8 字节：A 槽 CRC + B 槽 CRC */
#define UDS_DID_CAPABILITY        0xF184U   /* 能力位图：位0=支持0x34/36/37，位1=支持31例程，位2=双槽A/B */

/* WriteDataByIdentifier：OTA 相关 DID */
#define UDS_DID_BOOT_REQ          0xF190U   /* 1 字节 01 = 置"下次进 Bootloader"请求标志 */

/* 新增 NRC（ISO 14229-1 标准码，不要自造） */
#define UDS_NRC_GENERAL_REJECT    0x10U
#define UDS_NRC_SERVICE_NOT_SUP   0x11U
#define UDS_NRC_SUBFUNC_NOT_SUP   0x12U
#define UDS_NRC_INCORRECT_MSG_LEN 0x13U
#define UDS_NRC_CONDITIONS        0x22U
#define UDS_NRC_REQUEST_RANGE     0x31U
#define UDS_NRC_SECURITY_DENIED   0x33U
#define UDS_NRC_INVALID_KEY       0x35U
#define UDS_NRC_EXCEED_ATTEMPTS   0x36U
#define UDS_NRC_REQUIRED_TIME     0x37U
#define UDS_NRC_UPLOAD_DOWNLOAD_NOK 0x70U   /* 0x34/0x36 专用失败码 */
#define UDS_NRC_WRONG_BLOCK_SN    0x73U

/*============================ 传输层块序号约定（三方必须一致） ===========================*/
/* 0x36 的 blockSeq 取值 1..255，**255 之后回绕到 1**（不是 0）；0 永远非法（NRC 0x73）。
 * 断点续传时首块序号仍按同一公式计算：seq = ((blockIndex - 1) % 255) + 1
 * （唯一例外：整段只有一个块时 seq = 1）。
 * ⚠️ 其中 blockIndex 是**绝对块号**（从本次镜像的第 1 块算起），不是"本次 0x34 之后的第几块"。
 *    设备端必须由 0x34 的 addr 偏移反推：blockIndex = (addr - 槽APP基址) / 块大小。
 *    若设备把续传首块当成第 1 块（blockCount 清零），上位机按绝对块号发 seq 就会
 *    全部不匹配 → 必回 NRC 0x73，续传功能形同虚设。
 * 序号不符时设备回 NRC 0x73，上位机应**按设备期望的序号重发**该块。 */
#define UDS_BLOCK_SEQ_MIN         1U
#define UDS_BLOCK_SEQ_MAX         255U
#define UDS_BLOCK_SEQ_WRAP_TO     1U
#define UDS_BLOCK_SEQ_OF(idx1based) \
  ((uint8_t)((((uint32_t)(idx1based) - 1U) % (uint32_t)UDS_BLOCK_SEQ_MAX) + UDS_BLOCK_SEQ_WRAP_TO))

/*============================ 刷写地址与续传约定 =========================================*/
/* 【冻结】镜像头由"设备侧在 FF02 激活时合成"，上位机只传净固件（净荷流）。
 *
 * 0x34 RequestDownload：
 *   addr = 目标槽 APP 链接基址 = 槽基址 + 0x80（Slot A: 0x0800C080 / Slot B: 0x08040880）
 *          —— 即**从 APP 链接基址开始写净荷，槽首 32B 不在传输流内**
 *   size = 净固件字节数 = 镜像头 payloadSize
 * 断点续传：addr = 槽 APP 基址 + N*blockSize，size = 剩余长度（原样重收尾部）
 *
 * FF02（激活）时由设备按已收数据合成槽首 32B 镜像头：
 *   magic=OTA_IMG_MAGIC, imgSize=32+total, payloadSize=total, loadAddr=槽 APP 基址,
 *   payloadCrc = 0x37 携带的整镜像 CRC（若未携带则用本次累计 CRC）, hdrCrc=对[0,24)计算,
 *   version 取 0x37 可选携带、缺省 0。
 *
 * 因此 **0x37 RequestTransferExit 强烈建议携带 4 字节大端整镜像 CRC32**：
 *   [37, crc32(4B 大端)] —— 它同时充当 FF01 的参考值与合成镜像头的 payloadCrc。
 * 不携带时设备只能按"长度校验"通过（会在日志中告警）。
 *
 * 另一种被显式拒绝的约定：把 32B 镜像头放在传输流里、从槽基址起写。
 * 理由：会让 0x34 的 addr 与"APP 链接基址"语义冲突，并需要额外区分两种写偏移，
 * 收益为零（头只占 32B，设备合成更可靠）。 */
/* 传输层单条应用报文上限。
 * 本方案的块大小全部由它推导，而它又必须与应用层 `Drivers/User/Inc/isotp.h` 的
 * ISOTP_MAX_MSG 完全一致 —— 三方（BL / APP / 上位机）都以本宏为准：
 *
 *   AS_DELIVERED = 128  → 131 072 字节镜像约 164 秒（单块 125 B，1 048 块）
 *   OPTIMIZED    = 1024 → 131 072 字节镜像约 21 秒（单块 1016 B，130 块）  ← 默认采用
 *
 * 为什么可以放大：`isotp.c` 的收发缓冲都是 `uint8_t [ISOTP_MAX_MSG]`，各占 1 KB，
 * 放大到 1024 只多占 RAM 2 KB（本机 128 KB，实际使用约 10~20 KB）；
 * 且 `uds.c` 的应答缓冲必须同步按此值定尺（见 02 号文档的补丁），
 * 否则多字节 DID 应答会被静默截断。 */
#define OTA_ISOTP_MAX_MSG         1024U

/* 单块净荷上限 = ISO-TP 报文上限 − 1(SID) − 1(blockSeq) − 1(余量)，再向下对齐到 8 字节。
 *
 * ⚠️ 血线 5（2026-10-07 审查补）：**块大小必须是 8 的倍数**，否则断点续传不可用。
 *   原因：STM32G4 Flash 只支持 8 字节（双字）编程，`bl_diag.c` 的 BL_DiagRangeOk 因此
 *   要求续传起始地址 8 字节对齐。而续传偏移 = (N-1) × 块大小，若块大小不是 8 的倍数，
 *   则 8 个续传起点里有 7 个落在非 8 对齐地址上，**全部被设备回 NRC 0x31 拒绝**。
 *   实测反面案例：块大小取 1021（= 1024-3，mod 8 = 5）时，--resume 17 通过、
 *   18~22 全被拒；上位机只报 0x31，完全无法定位原因。
 *   取 ((1024 - 3) & ~7) = 1016：1016 + 2(SID+seq) = 1018 <= 1024 仍成立，
 *   吞吐相对 1021 只损失 0.5%（131 072 字节镜像 21 秒 → 21.1 秒），
 *   换来的是**任意续传起点都合法**。
 * 推导式写死为位运算而非字面量，保证改动 OTA_ISOTP_MAX_MSG 后自动重新对齐。 */
#define UDS_DL_MAX_BLOCK_PAYLOAD  ((OTA_ISOTP_MAX_MSG - 3U) & ~7U)   /* = 1016 */
#define UDS_MAXLEN_FIELD          UDS_DL_MAX_BLOCK_PAYLOAD   /* 0x34 正应答通告的 maxNumberOfBlockLength */
#define UDS_XFEREXIT_WITH_CRC     1U     /* 约定 0x37 携带 4B 整镜像 CRC32（长度 5） */

/* 编译期一致性：契约值与应用层实际编译进去的 isotp.h 必须对齐，
 * 否则会出现"上位机按 1021 发、设备只收 128"的静默溢流失败。
 * app 侧包含本头文件时 isotp.h 已在作用域内；BL 侧由 bl_main.h 提供同值。 */
#if defined(ISOTP_MAX_MSG) && (ISOTP_MAX_MSG != OTA_ISOTP_MAX_MSG)
#error "ota_common.h 的 OTA_ISOTP_MAX_MSG 与应用层 isotp.h 的 ISOTP_MAX_MSG 不一致"
#endif
#if (UDS_DL_MAX_BLOCK_PAYLOAD < 125U)
#error "单块净荷不得低于 125 字节（会显著劣化吞吐）"
#endif
/* 血线 5：块大小必须 8 字节倍数，否则断点续传起点大面积落在非对齐地址上被拒（见上方说明） */
#if ((UDS_DL_MAX_BLOCK_PAYLOAD % 8U) != 0U)
#error "UDS_DL_MAX_BLOCK_PAYLOAD 必须是 8 字节倍数：Flash 双字编程要求续传地址 8 对齐，块大小非 8 倍数会使绝大多数续传起点被 NRC 0x31 拒绝"
#endif
/* 块净荷 + SID + blockSeq 不得超过 ISO-TP 单条报文上限 */
#if ((UDS_DL_MAX_BLOCK_PAYLOAD + 2U) > OTA_ISOTP_MAX_MSG)
#error "单块净荷 + 2 字节 UDS 头超过 OTA_ISOTP_MAX_MSG"
#endif
/* ISO-TP 首帧的长度字段为 12 位，上限 4095：单块净荷 + 3 字节 UDS 头必须留在其内 */
#if (OTA_ISOTP_MAX_MSG > 4095U)
#error "ISO-TP 单条报文不得超过 12 位长度上限 4095"
#endif

/* 0x34 RequestDownload 请求体长度：34 00 44 + addr(4B 大端) + size(4B 大端) = 11 字节。
 * ⚠️ 血线 6（2026-10-07 审查补）：曾把设备侧校验写成 10，而代码实际已访问 req[10]，
 *    APP 侧与上位机都是 11 —— 真机上 0x34 恒回 NRC 0x13，一块固件都刷不进去，
 *    而上位机单元测试全跑在模拟器上（模拟器是 11），永远测不到。
 *    三方一律引用本宏，禁止再写裸数字。 */
#define UDS_DL_REQ_LEN             11U
/* 0x31 01 FF00（显式擦除）请求体：31 01 FF 00 = 4 字节；带 size 时 6 字节。
 * ⚠️ size 字段只有 16 位（上限 65535 < 单槽净荷 212864），**无法表达整槽擦除**；
 *    且 0x34 已隐式擦除目标槽，故 FF00 属冗余路径，上位机不再调用。 */
#define UDS_RID_ERASE_REQ_LEN_MIN 4U
#define UDS_RID_ERASE_REQ_LEN_MAX 6U

/* 0x31 01 FF01（校验）正应答：[71, 01, FF, 01, status, crc32(4B 大端)] = 9 字节
 *   status: 0x00 通过 / 0x01 CRC 不符 / 0x02 长度不符
 *   crc32 : 设备对本轮实际接收字节流重算的 CRC32（供上位机交叉核对）
 * ⚠️ 2026-10-07 修正（M8）：UDS_VERIFY_RESP_MIN_LEN 由 4 改为 **5**。
 *    status 位于索引 4（第 5 字节），4 字节的 `[71,01,FF,01]` **根本不含 status**。
 *    旧契约写"设备若只回 4 字节亦视为合法"，等于允许把截断应答当成"校验通过"，
 *    会让 CRC 校验被静默跳过。设备实现始终回 status（见 bl_diag.c 的 resp[4]），
 *    最小合法长度就是 5。 */
#define UDS_VERIFY_RESP_FULL_LEN  9U
#define UDS_VERIFY_RESP_MIN_LEN   5U

/*===================================== OTA 状态机 ========================================*/
/* 状态取值刻意做成"两个槽互为备份"的对称逻辑：
 *   IDLE_A/IDLE_B  = 稳态，该槽已在现场验证过，可回滚的对方槽也有效或已失效
 *   TRIAL_A/TRIAL_B= 新固件首次运行，尚未被 APP 确认（APP 需调用 0x31 01 FF04 落定） */
#define OTA_ST_IDLE_A             0x00U
#define OTA_ST_IDLE_B             0x01U
#define OTA_ST_TRIAL_A            0x02U
#define OTA_ST_TRIAL_B            0x03U
#define OTA_ST_STATE_MAX          0x04U

/* 试运行允许的复位次数：超过则判定新固件"起不来"，自动回滚到另一槽。
 * 取值依据：需容忍"上电后很快掉电"的正常抖动，又要避免无限重试耗死现场设备。 */
#define OTA_TRIAL_BOOT_LIMIT      3U

/* 控制区魔数：主份与镜像份各自独立校验，任一有效即可启动 */
#define OTA_CFG_MAGIC             0x4734424CU   /* "G4BL" */

/*===================================== 控制区（参数区）结构 =============================*/
/* 必须保持紧凑且不含指针，字段顺序即 Flash 布局；两份副本 + 单调 seq 构成掉电安全的写策略：
 *   写入流程：读取两份->选 seq 大者为当前->在"另一份"所在偏移写 seq+1 新内容->校验回读。
 *   掉电只会破坏正在写的那一份，另一份始终是完整旧值。 */
typedef struct
{
  uint32_t magic;             /* OTA_CFG_MAGIC，非法则本副本作废 */
  uint32_t seq;               /* 单调递增序号，用于在两份副本中选新 */
  uint32_t state;             /* OTA_ST_xxx */
  uint32_t trialLeft;         /* 试运行剩余复位次数（仅 TRIAL 状态有意义） */
  uint32_t slotCrc[2];        /* [0]=Slot A 净固件 CRC32，[1]=Slot B */
  uint32_t slotSize[2];       /* [0]=Slot A 净固件字节数，[1]=Slot B */
  uint32_t slotVersion[2];    /* [0]=Slot A 版本(0xMMmm0000)，[1]=Slot B */
  uint32_t lastError;         /* 最近一次失败原因（见 OTA_ERR_xxx），现场诊断用 */
  uint32_t crc;               /* 对 [0, offsetof(crc)) 计算的 CRC32 */
} OTA_Cfg;

/* sizeof(OTA_Cfg) = 12 × u32 = 48 字节（成员全是 uint32_t，故结构无填充，布局即字段顺序）：
 *   magic(0) seq(4) state(8) trialLeft(12) slotCrc[2](16) slotSize[2](24)
 *   slotVersion[2](32) lastError(40) crc(44)
 * offsetof(crc) = 44；48 是 8 的倍数，双字编程天然对齐。
 *
 * ⚠️ 三方（Bootloader / APP / 上位机）一律用 sizeof / offsetof，**禁止写任何字节数字面量**：
 * 曾出现过把"9 个字段"误当成"9 个 u32"而写成 36 的错误，若照错值读写记录，
 * lastError(40) 与 crc(44) 会落在未写入区，配置副本校验必然失败且**静默**。
 * 因此下面把真实尺寸与 crc 偏移一并锁死。
 *
 * 为什么用"负数组长度"而不是 `#if (sizeof(OTA_Cfg) != 48)`：
 * `sizeof`/`offsetof` 能否出现在预处理 `#if` 表达式中属于实现自定义行为，
 * ARMCC 等工具链上不保证可用。负数组长度是 C90 起就标准的编译期断言写法，
 * 在 ARMCC / GCC / Clang 上行为完全一致，且报错能定位到本行。 */
typedef char OtaCfg_SizeAssert[(sizeof(OTA_Cfg) == 48U) ? 1 : -1];
typedef char OtaCfg_CrcOffAssert[(offsetof(OTA_Cfg, crc) == 44U) ? 1 : -1];
typedef char OtaCfg_AlignAssert[((sizeof(OTA_Cfg) % 8U) == 0U) ? 1 : -1];
typedef char OtaCfg_OffAlignAssert[(((OTA_CFG_SLOT_OFFSET % 8U) == 0U) &&
                                    ((OTA_CFG_SLOT_MIRROR_OFF % 8U) == 0U)) ? 1 : -1];

/* 血线 4（2026-10-05 用一块变砖的板子换来）：**APP 链接基址必须 0x80 字节对齐**。
 * 依据：Cortex-M4 `SCB->VTOR[6:0]` 是 RES0（写入被忽略）—— 向量表只能 0x80 对齐。
 * 反面教材：保留区曾是 32，于是 APP 基址 = 0x0800C020，写进 VTOR 只剩 0x0800C000，
 *           APP 一开中断就按【镜像头】取向量 -> INVSTATE/IBUSERR -> lockup。
 * 这条断言把"槽基址 + 保留区"的对齐关系锁在编译期，改分区表或保留区都会立刻报错。 */
typedef char OtaAppBaseAlignAssert[(((OTA_SLOT_A_ADDR + OTA_IMG_HDR_SIZE) % 0x80U) == 0U) &&
                                   (((OTA_SLOT_B_ADDR + OTA_IMG_HDR_SIZE) % 0x80U) == 0U) ? 1 : -1];

/* lastError 取值 */
#define OTA_ERR_NONE              0x00000000U
#define OTA_ERR_HDR_MAGIC         0x00000001U   /* 镜像头魔数错 */
#define OTA_ERR_HDR_CRC           0x00000002U   /* 镜像头自校验失败 */
#define OTA_ERR_PAYLOAD_CRC       0x00000003U   /* 净固件 CRC 不符 */
#define OTA_ERR_SIZE_RANGE        0x00000004U   /* 长度越界 */
#define OTA_ERR_LOAD_ADDR         0x00000005U   /* 载入地址与目标槽不符 */
#define OTA_ERR_ERASE             0x00000006U   /* 擦除失败 */
#define OTA_ERR_PROGRAM           0x00000007U   /* 编程失败 */
#define OTA_ERR_SP                0x00000008U   /* 栈顶指针非法（跳转前检查） */
#define OTA_ERR_RESET_VEC         0x00000009U   /* 复位向量非法/不在槽内 */
#define OTA_ERR_TRANSFER          0x0000000AU   /* 传输中断/超时 */

/*===================================== 编译期自检 ========================================*/
#if (OTA_BL_CODE_ADDR % OTA_FLASH_PAGE_SIZE) != 0
#error "OTA_BL_CODE_ADDR 必须页对齐"
#endif
/* 血线 1：Bootloader 代码区必须完全落在 Slot A 之前，否则下载 A 槽会擦掉 BL */
#if (OTA_BL_CODE_ADDR + OTA_BL_CODE_SIZE) > OTA_SLOT_A_ADDR
#error "Bootloader 代码区与 Slot A 重叠：下载 A 槽会擦掉 Bootloader"
#endif
/* 血线 2：控制区两份副本都必须完整落在控制区页内，且不得压到页首向量表。
 * 反面教材（曾经的错误）：把副本1 放在 +0x800 —— 那正好等于页大小，
 * 副本1 会落到下一页（PRIMARY 页的下一页 = 0x08000800 是 BL 代码首地址；
 * MIRROR 页的下一页 = 0x08040800 是 Slot B 首地址），写入必然越界/破坏 BL。
 * （偏移的 8 字节对齐断言已在前文以 typedef 形式给出，此处不再重复。） */
#if (OTA_CFG_SLOT_OFFSET < OTA_CFG_VECTAB_BYTES)
#error "控制区副本0 会覆盖 BL 向量表"
#endif
#if ((OTA_CFG_SLOT_MIRROR_OFF + 256U) > OTA_CFG_PAGE_SIZE)
#error "控制区副本1 跨出控制区页（会落到下一页的 BL 代码区/Slot B 首部）"
#endif
#if ((OTA_CFG_SLOT_OFFSET + 256U) > OTA_CFG_SLOT_MIRROR_OFF)
#error "控制区两份副本间距过小，同页擦写时无法互相保全"
#endif
#if (OTA_SLOT_A_ADDR % OTA_FLASH_PAGE_SIZE) != 0
#error "OTA_SLOT_A_ADDR 必须页对齐"
#endif
#if (OTA_SLOT_B_ADDR % OTA_FLASH_PAGE_SIZE) != 0
#error "OTA_SLOT_B_ADDR 必须页对齐"
#endif
#if (OTA_SLOT_SIZE % OTA_FLASH_PAGE_SIZE) != 0
#error "OTA_SLOT_SIZE 必须是页大小整数倍"
#endif
/* 血线 3：两个 APP 槽都不得跨越 Bank 边界。
 * 原因：DBANK 是全局选项字节，跨 Bank 的代码在"擦写本槽所在 Bank"时会取指到正在擦除的
 * Bank -> 读出 0xFF/未定义 -> HardFault。因此真正的约束是**包含关系**（⊂ 某个 Bank），
 * 而不是"起始地址精确对齐 Bank 起始"。
 *   Slot A: [0x0800C000, 0x08040000) ⊂ Bank1 [0x08000000, 0x08040000)
 *   Slot B: [0x08040800, 0x08078800) ⊂ Bank2 [0x08040000, 0x08080000)
 * （Slot B 故意不贴 0x08040000，是为了给该页留作控制区镜像份 BL_HEADER_MIR。） */
#if (OTA_SLOT_A_ADDR < OTA_FLASH_BASE) || \
    ((OTA_SLOT_A_ADDR + OTA_SLOT_SIZE) > (OTA_FLASH_BASE + OTA_FLASH_BANK_SIZE))
#error "Slot A 跨越 Bank1 边界，DBANK 模式下会 HardFault"
#endif
#if (OTA_SLOT_B_ADDR < (OTA_FLASH_BASE + OTA_FLASH_BANK_SIZE)) || \
    ((OTA_SLOT_B_ADDR + OTA_SLOT_SIZE) > (OTA_FLASH_BASE + OTA_FLASH_SIZE))
#error "Slot B 必须完全落在 Bank2 内（且不越过 Flash 末尾），否则取指跨越 Bank 边界"
#endif

#endif /* __OTA_COMMON_H */
