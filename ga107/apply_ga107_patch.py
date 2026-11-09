#!/usr/bin/env python3
"""Offline experimental patch for upstream nv-uefi-unlocker; not a tested unlock."""
import argparse
from pathlib import Path

def one(s, old, new, name):
    n = s.count(old)
    if n != 1:
        raise RuntimeError(f'{name}: expected 1 source anchor, got {n}. Upstream drift; refusing.')
    return s.replace(old, new, 1)

def transform_booter(s):
    if 'PlmFlipAt' in s:
        raise RuntimeError('Already patched bootloader; refusing')
    s = one(s, '    PlmFlip,\n}',
            '    PlmFlip,\n    /// Experimental GA107 RECONFIG second-fire gate.\n    PlmFlipAt(u32),\n}', 'BooterWait enum')
    s = one(s, '        BooterWait::PlmFlip => {', '''        BooterWait::PlmFlip | BooterWait::PlmFlipAt(_) => {
            let plm_addr = match wait {
                BooterWait::PlmFlipAt(addr) => addr,
                _ => crate::tx::regs::FEAT_PLM,
            };''', 'PLM wait branch')
    start = s.index('        BooterWait::PlmFlip | BooterWait::PlmFlipAt(_) => {')
    end = s.index('            verdict\n        }', start)
    region = s[start:end]
    old = 'crate::tx::regs::FEAT_PLM as u64'
    if region.count(old) != 2:
        raise RuntimeError(f'PLM polling reads expected 2, found {region.count(old)}')
    return s[:start] + region.replace(old, 'plm_addr as u64') + s[end:]

INSERT = r'''    // GA107 RECONFIG proof-of-concept: second signed-booter SEC2 fire.
    // Explicitly opt-in by marker; no effects on the upstream default path.
    if booter_ok && payload_mode && assets.family == "ga102"
        && probe_file("\\tx-ga107-reconfig.txt")
    {
        let id = gpu.pci.config_read_u32(0);
        let rplm = gpu.read32_raw(0x0082_00d8);
        let st0 = gpu.read32_raw(0x0082_0c38);
        let st1 = gpu.read32_raw(0x0082_0c3c);
        let ov0 = gpu.read32_raw(0x0082_0a40);
        let ov1 = gpu.read32_raw(0x0082_0a44);
        info!("GA107-RC: preflight id={id:#010x} plm={rplm:#010x} status0/1={st0:#x}/{st1:#x} ovr0/1={ov0:#x}/{ov1:#x}");
        if id != 0x25ad_10de || vram_fb || rplm != 0xffff_ff8f
            || st0 != 0x21 || st1 != 0x21 || ov0 != 0 || ov1 != 0
        {
            info!("GA107-RC: ABORT second stage (baseline/identity/staging mismatch)");
        } else if let Some(v67_original) = assets.v67.as_ref() {
            const ADDR_OFF: usize = 0xf960;
            // Parent loader validated the payload length earlier.
            let orig = u32::from_le_bytes(v67_original[ADDR_OFF..ADDR_OFF + 4].try_into().unwrap());
            if v67_original.len() != tb::V67_SIZE || orig != regs::FEAT_PLM {
                info!("GA107-RC: ABORT unrecognized payload");
            } else {
                // Stage 1 already unlocked FEAT/Tensor. Retarget the DMA
                // signature for a second SEC2 execution in the SAME boot.
                let sig = unsafe { core::slice::from_raw_parts_mut(sig_dma.host, tb::V67_SIZE) };
                sig[ADDR_OFF..ADDR_OFF + 4].copy_from_slice(&0x0082_00d8u32.to_le_bytes());
                core::sync::atomic::fence(core::sync::atomic::Ordering::SeqCst);
                info!("GA107-RC: second SEC2 fire targeting 0x8200d8");
                let secondary = tb::booter_fire(gpu, &hdr, booter_view, meta_addr,
                    false, tb::BooterWait::PlmFlipAt(0x0082_00d8));
                sig[ADDR_OFF..ADDR_OFF + 4].copy_from_slice(&regs::FEAT_PLM.to_le_bytes());
                core::sync::atomic::fence(core::sync::atomic::Ordering::SeqCst);
                let now = gpu.read32_raw(0x0082_00d8);
                info!("GA107-RC: second booter result={secondary:?}, RECONFIG_PLM={now:#010x}");
                if secondary.is_ok() && now == 0xffff_ffff {
                    // CMP170HX uses 0xff. The GA107 bit-mask semantics are
                    // UNVERIFIED. Limit this to bit0 on the two actual GPCs.
                    for g in 0..2u32 {
                        let reg = 0x0082_0a40 + 4 * g;
                        let stat = 0x0082_0c38 + 4 * g;
                        let old = gpu.read32_raw(reg);
                        let pre = gpu.read32_raw(stat);
                        if old != 0 || pre != 0x21 { continue; }
                        gpu.write32(reg, 1);
                        let rb = gpu.read32_raw(reg);
                        let post = gpu.read32_raw(stat);
                        info!("GA107-RC: GPC{g} override=1 readback={rb:#x} STATUS {pre:#x}->{post:#x}");
                        if post & 1 != 0 || (post & !1) != (pre & !1) {
                            gpu.write32(reg, old);
                            info!("GA107-RC: GPC{g} no isolated TPC0 improvement; restore readback={:#x}", gpu.read32_raw(reg));
                        }
                    }
                } else {
                    info!("GA107-RC: protected gate did not open — no override writes");
                }
            }
        }
    }
'''

def transform_tx(s):
    if 'GA107-RC:' in s:
        raise RuntimeError('Already patched transaction; refusing')
    needle = '    if payload_mode {\n        if booter_ok {\n            // Isolation experiment'
    s = one(s, needle, INSERT + needle, 'transaction insertion')
    anchor = '        info!("POST-FLR: verified{}", if flr_expect_unlock { " (selectors survive AON)" } else { "" });'
    post = r"""
        if assets.family == "ga102" && gpu.pci.config_read_u32(0) == 0x25ad_10de
            && probe_file("\\tx-ga107-reconfig.txt")
        {
            let a = gpu.read32_raw(0x0082_0c38);
            let b = gpu.read32_raw(0x0082_0c3c);
            info!("GA107-RC: POST-FLR GPC0 STATUS={a:#x} GPC1 STATUS={b:#x}; still requires Windows SM-count confirmation");
        }
    """
    return one(s, anchor, anchor + post, 'post-FLR readback')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('checkout', type=Path)
    args = p.parse_args()
    base = args.checkout / 'loader' / 'src'
    files = [base / 'tx.rs', base / 'tx_booter.rs']
    if any(not f.is_file() for f in files):
        raise SystemExit('Expected loader/src/tx.rs and loader/src/tx_booter.rs')
    sources = [f.read_text(encoding='utf8') for f in files]
    try:
        if 'GA107-RC:' in sources[0] or 'PlmFlipAt' in sources[1]:
            raise RuntimeError('Already patched')
        patched = [transform_tx(sources[0]), transform_booter(sources[1])]
        backups = [f.with_name(f.name + '.ga107-v07-original') for f in files]
        if any(b.exists() for b in backups):
            raise RuntimeError('Backup already exists')
    except Exception as e:
        raise SystemExit(f'ABORT before any changes: {e}')
    for f,b,s,out in zip(files,backups,sources,patched):
        b.write_text(s, encoding='utf8')
        f.write_text(out, encoding='utf8')
        print('patched', f)
    print('SOURCE PATCH APPLIED; not compiled or tested on hardware.')
if __name__ == '__main__':
    main()
