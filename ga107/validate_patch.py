from pathlib import Path
import tempfile, importlib.util
spec=importlib.util.spec_from_file_location('mod',Path(__file__).with_name('apply_ga107_patch.py'))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def tx():
 return '''    if payload_mode {\n        if booter_ok {\n            // Isolation experiment\n            placeholder\n        }\n    }\n        info!("POST-FLR: verified{}", if flr_expect_unlock { " (selectors survive AON)" } else { "" });\n'''

def booter():
 return '''pub enum BooterWait {\n    Halt,\n    PlmFlip,\n}\n        BooterWait::PlmFlip => {\n            let entry = gpu.pci.bar_read_u32(gpu.bar, crate::tx::regs::FEAT_PLM as u64);\n            let plm = gpu.pci.bar_read_u32(gpu.bar, crate::tx::regs::FEAT_PLM as u64);\n            verdict\n        }\n'''
with tempfile.TemporaryDirectory() as d:
 root=Path(d); base=root/'loader'/'src'; base.mkdir(parents=True)
 (base/'tx.rs').write_text(tx()); (base/'tx_booter.rs').write_text(booter())
 m.main if False else None
 patched_tx=m.transform_tx(tx()); patched_booter=m.transform_booter(booter())
 assert 'GA107-RC: second SEC2 fire' in patched_tx
 assert 'PlmFlipAt' in patched_booter
 assert patched_booter.count('plm_addr as u64')==2
 try:
  m.transform_tx(patched_tx)
 except RuntimeError:
  pass
 else:
  raise AssertionError('Patch must refuse already modified source')
 print('PASS: source transformations work on minimal anchor fixtures')
 print('WARN: this does not prove compatibility with upstream, Rust compilation, or GA107 hardware')

assert 'WRITE EXPERIMENT OPTED IN' in patched_tx
assert 'live signature differs' in patched_tx
assert 'original payload restored' in patched_tx
assert 'gpu.write32(reg, 0x01)' in patched_tx
assert 'intentionally retained until FLR' in patched_tx
assert 'rollback_required' in patched_tx
print('PASS: live DMA guard, payload restore, explicit opt-in, guarded retained write and rollback')
assert 'gpu.write32(reg, before)' not in patched_tx
assert 'GA107-RC: POST-FLR STATUS0/1=' in patched_tx
assert 'Some(regs::FEAT_PLM)' in patched_tx
print('PASS: persistent write is gated; no unconditional restore; post-FLR readback added')
