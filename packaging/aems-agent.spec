# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the AEMS Local Bridge Agent.

Builds a single-folder distribution with only the agent's dependencies
(uvicorn, fastapi, pydantic, typer) — excludes Flask, SQLAlchemy,
grading engine, and LLM providers to keep the bundle small.

Usage:
    pyinstaller packaging/aems-agent.spec
"""

import sys
from pathlib import Path

block_cipher = None

# Project root (one level up from this spec file)
PROJECT_ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(PROJECT_ROOT / 'src' / 'aems_agent' / 'cli.py')],
    pathex=[str(PROJECT_ROOT / 'src')],
    binaries=[],
    datas=[],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'pystray',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        # Required by grading_bundle.py (direct) and annotate.py (via aems_pdf_annotator._fitz trampoline).
        'fitz',
        'pymupdf',
        'aems_pdf_annotator',
        'aems_pdf_annotator._fitz',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude the full AEMS web app and heavy dependencies
        'flask',
        'flask_login',
        'flask_wtf',
        'sqlalchemy',
        'alembic',
        'redis',
        'celery',
        'anthropic',
        'openai',
        'google.generativeai',
        'ollama',
        'tesseract',
        'pytesseract',
        'playwright',
        'matplotlib',
        'pandas',
        'numpy',
        'scipy',
        'sklearn',
        'torch',
        'tensorflow',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='aems-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False if sys.platform == 'win32' else True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'packaging' / 'icon.ico')
    if (PROJECT_ROOT / 'packaging' / 'icon.ico').exists()
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='aems-agent',
)
