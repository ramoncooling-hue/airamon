from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_submodules, copy_metadata, collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
from scripts.check_extension_packaging import collect_macos_extensions, collect_macos_asr
extension_data, extension_binaries, extension_hidden = collect_macos_extensions(root)
asr_data, asr_binaries, asr_hidden = collect_macos_asr()
extension_data += asr_data
extension_binaries += asr_binaries
extension_hidden += asr_hidden
extension_data += collect_data_files('pypdfium2_raw') + copy_metadata('pypdfium2')
extension_binaries += collect_dynamic_libs('pypdfium2_raw')
extension_hidden += ['pypdfium2', 'pypdfium2_raw']
data = [
    (str(root / 'app/schema'), 'app/schema'),
    (str(root / 'web'), 'web'),
    (str(root / 'seed/public'), 'seed'),
    (str(root / 'skills'), 'skills'),
    (str(root / 'agents'), 'agents'),
    (str(root / 'domains'), 'domains'),
    (str(root / 'docs/platform'), 'docs/platform'),
    (str(root / 'VERSION'), '.'),
    (str(root / 'LICENSE'), '.'),
]
for package in ('fastapi', 'uvicorn', 'pydantic', 'starlette', 'python-docx', 'python-pptx', 'pypdf'):
    data += copy_metadata(package)
a = Analysis(
    [str(root / 'desktop/backend.py')], pathex=[str(root)], binaries=extension_binaries, datas=data + extension_data,
    hiddenimports=collect_submodules('app') + collect_submodules('uvicorn')
        + ['scripts.load_memory', 'scripts.migrate_runtime', 'scripts.backup_runtime', 'pypdf', 'docx', 'pptx', 'certifi']
        + extension_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[str(root / 'scripts/check_extension_packaging.py')],
    excludes=['pytest', 'tkinter', 'matplotlib', 'scipy', 'IPython', 'PyInstaller'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='mindock-backend',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, target_arch='arm64', codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='mindock-backend')
