# -*- mode: python ; coding: utf-8 -*-
# onedir 打包：免 onefile 每次解压到 %TEMP%，启动快；目录可整体 zip 分发。
# 体积裁剪：去掉软渲染 OpenGL、Qt 未用模块（Pdf/Network/Svg）、多余图片格式插件与翻译。
import os


a = Analysis(
    ['spectrum.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 不要加 ssl/unittest 等 excludes：numpy.random 经 _pickle.py 隐式依赖 secrets，
    # 裁 stdlib 模块会炸启动（体积大头在二进制过滤里，excludes 只省 1-2 MB）。
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)

# ---- 裁剪无用二进制（TOC 项为 (dest_name, src_name, typecode)）----
import fnmatch

DROP_BINARIES = [
    '*opengl32sw.dll',                       # 软件 OpenGL 回退，7.7 MB，仅无显卡驱动机器需要
    '*Qt6Pdf.dll', '*Qt6Network.dll', '*Qt6Svg.dll',
    '*platforms/qminimal.dll', '*platforms/qoffscreen.dll',
    '*generic/*', '*iconengines/*',
    '*imageformats/qicns.dll', '*imageformats/qjpeg.dll',
    '*imageformats/qpdf.dll', '*imageformats/qsvg.dll',
    '*imageformats/qtga.dll', '*imageformats/qtiff.dll',
    '*imageformats/qwbmp.dll', '*imageformats/qwebp.dll',
    '*translations/*',
]

def _keep(entry):
    name = entry[0].replace('\\', '/').lower()
    return not any(fnmatch.fnmatch(name, pat.lower()) for pat in DROP_BINARIES)

before = len(a.binaries)
a.binaries = [b for b in a.binaries if _keep(b)]
print(f'binaries: {before} -> {len(a.binaries)} (dropped {before - len(a.binaries)})')

# .qm 翻译等是 DATA，走 a.datas，单独过滤
before = len(a.datas)
a.datas = [d for d in a.datas if _keep(d)]
print(f'datas: {before} -> {len(a.datas)} (dropped {before - len(a.datas)})')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='spectrum',
    icon=os.path.join(SPECPATH, 'icon.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='spectrum',
)
