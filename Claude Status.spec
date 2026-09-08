# -*- mode: python ; coding: utf-8 -*-

# 用钥匙串里的自签名代码签名证书，而不是 ad-hoc。
# ad-hoc 签名的 designated requirement 是 `cdhash H"..."`，每次重新打包都变，
# macOS 就把 app 当成另一个，「辅助功能」授权静默失效（列表里勾还在但不生效，
# 报错退化成点名 osascript：“osascript”不允许辅助访问 -1728）。
# 自签名后 DR 变成 `identifier <bundle id> and certificate leaf = H"<证书>"`，
# 跨重建稳定，授权一次就够。
# 证书用「钥匙串访问 → 证书助理 → 创建证书」建：自签名根证书 + 代码签名用途；
# 不需要设为信任（codesign 不校验信任链）。
CODESIGN_IDENTITY = 'Claude Status Signing'
BUNDLE_ID = 'com.kaibinwa.claude-status'
ENTITLEMENTS = 'entitlements.plist'   # 关掉库验证，见该文件内注释


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6.QtNetwork', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtMultimedia', 'PyQt6.QtDBus', 'PyQt6.QtSvg', 'PyQt6.QtTest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Claude Status',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Claude Status',
)
app = BUNDLE(
    coll,
    name='Claude Status.app',
    icon=None,
    bundle_identifier=BUNDLE_ID,
    info_plist={'LSUIElement': True},
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
)
