#!/usr/bin/env python3
"""GA107 v0.11 experimental persistent RECONFIG override; untested on hardware."""
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

INSERT = r'''    // GA107 RECONFIG second SEC2 fire: explicit opt-in, no TPC override writes.
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
            // Check bounds before indexing the firmware payload.
            let orig = v67_original.get(ADDR_OFF..ADDR_OFF + 4)
                .and_then(|b| <[u8; 4]>::try_from(b).ok())
                .map(u32::from_le_bytes);
            if v67_original.len() != tb::V67_SIZE || orig != Some(regs::FEAT_PLM) {
                info!("GA107-RC: ABORT unrecognized payload");
            } else {
                // Stage 1 already unlocked FEAT/Tensor. Retarget the DMA
                // signature for a second SEC2 execution in the SAME boot.
                let sig = unsafe { core::slice::from_raw_parts_mut(sig_dma.host, tb::V67_SIZE) };
                // Confirm the *live DMA buffer* has the expected original payload.
                // Do not trust an immutable source check alone.
                let live_before = u32::from_le_bytes(sig[ADDR_OFF..ADDR_OFF + 4].try_into().unwrap());
                if live_before != regs::FEAT_PLM {
                    info!("GA107-RC: ABORT live signature differs from expected FEAT target: {live_before:#010x}");
                    return Err(TxError::EInval);
                }
                sig[ADDR_OFF..ADDR_OFF + 4].copy_from_slice(&0x0082_00d8u32.to_le_bytes());
                core::sync::atomic::fence(core::sync::atomic::Ordering::SeqCst);
                info!("GA107-RC: second SEC2 fire targeting 0x8200d8");
                let secondary = tb::booter_fire(gpu, &hdr, booter_view, meta_addr,
                    false, tb::BooterWait::PlmFlipAt(0x0082_00d8));
                sig[ADDR_OFF..ADDR_OFF + 4].copy_from_slice(&regs::FEAT_PLM.to_le_bytes());
                core::sync::atomic::fence(core::sync::atomic::Ordering::SeqCst);
                let restored = u32::from_le_bytes(sig[ADDR_OFF..ADDR_OFF + 4].try_into().unwrap());
                if restored != regs::FEAT_PLM {
                    info!("GA107-RC: ERROR payload restoration check failed: {restored:#010x}");
                    return Err(TxError::EIO);
                }
                let now = gpu.read32_raw(0x0082_00d8);
                info!("GA107-RC: second booter result={secondary:?}, RECONFIG_PLM={now:#010x}; original payload restored");
                if secondary.is_ok() && now == 0xffff_ffff {
                    // GA107 EXPERIMENT: a successful register write is NOT proof of
                    // activating a previously fused-off SM. This mode explicitly
                    // leaves accepted RECONFIG bits set until the normal FLR,
                    // where we log whether the mask survived.
                    if probe_file("\\tx-ga107-tpc-write.txt") {
                        info!("GA107-RC: PERSISTENT WRITE EXPERIMENT OPTED IN (GA107 unknown semantics)");
                        let mut changed: [bool; 2] = [false, false];
                        let mut rollback_required = false;
                        for g in 0..2usize {
                            let reg = 0x0082_0a40 + 4 * g as u32;
                            let stat = 0x0082_0c38 + 4 * g as u32;
                            let before = gpu.read32_raw(reg);
                            let status_before = gpu.read32_raw(stat);
                            if before != 0 || status_before != 0x21 {
                                info!("GA107-RC: GPC{g} baseline unexpected: ovr={before:#x} status={status_before:#x}; rollback");
                                rollback_required = true;
                                break;
                            }
                            // Target ONLY the observed TPC0 candidate; do not
                            // use GA100's all-TPC 0xFF mask on GA107.
                            gpu.write32(reg, 0x01);
                            let readback = gpu.read32_raw(reg);
                            let status_after = gpu.read32_raw(stat);
                            info!("GA107-RC: GPC{g} write=0x01 OVR={readback:#010x} STATUS {status_before:#010x}->{status_after:#010x}");
                            if readback != 0x01 || status_after == 0xffff_ffff
                                || (status_after & 0xffff_0000) == 0xbadf_0000
                            {
                                info!("GA107-RC: GPC{g} REJECTED/POISON; rollback requested");
                                rollback_required = true;
                                break;
                            }
                            changed[g] = true;
                        }
                        if rollback_required {
                            for g in 0..2usize {
                                let reg = 0x0082_0a40 + 4 * g as u32;
                                // The failing write may partially stick, so
                                // restore both known-baseline registers.
                                gpu.write32(reg, 0);
                                let rb = gpu.read32_raw(reg);
                                info!("GA107-RC: rollback GPC{g} OVR={rb:#010x}");
                                if rb != 0 { return Err(TxError::EIO); }
                            }
                            info!("GA107-RC: no TPC override intentionally retained");
                        } else if changed[0] && changed[1] {
                            info!("GA107-RC: GPC0/GPC1 OVR accepted and intentionally retained until FLR; NOT an SM unlock verdict");
                        }
                    } else {
                        info!("GA107-RC: RECONFIG gate open; no write marker — no TPC modifications");
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
            let ov0 = gpu.read32_raw(0x0082_0a40);
            let ov1 = gpu.read32_raw(0x0082_0a44);
            info!("GA107-RC: POST-FLR STATUS0/1={a:#x}/{b:#x} OVR0/1={ov0:#x}/{ov1:#x}; Windows SM-count confirmation required");
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
