; AEMS Agent NSIS Installer Script
; Builds a Windows installer for the AEMS Local Bridge Agent

!include "MUI2.nsh"

; Default version when invoked without -DAGENT_VERSION (e.g., directly via NSIS).
; The build pipeline passes the real version from pyproject.toml at build time.
!ifndef AGENT_VERSION
    !define AGENT_VERSION "0.0.0-dev"
!endif

; General
Name "AEMS Agent ${AGENT_VERSION}"
OutFile "${OUTPUT_DIR}\aems-agent-setup.exe"
InstallDir "$LOCALAPPDATA\AEMS Agent"
InstallDirRegKey HKCU "Software\AEMS Agent" "InstallDir"
RequestExecutionLevel user

; UI
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_ABORTWARNING

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

; --- Finish page: offer to start the agent + open AEMS settings ---
!define MUI_FINISHPAGE_RUN "$INSTDIR\aems-agent.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "run --tray"
!define MUI_FINISHPAGE_RUN_TEXT "Start AEMS Agent now (recommended)"
; (default-checked; remove the next line to require opt-in)
; !define MUI_FINISHPAGE_RUN_NOTCHECKED

!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Open the AEMS settings in your browser"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenAemsSettings

!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Language
!insertmacro MUI_LANGUAGE "English"

; Installer section
Section "Install"
    SetOutPath "$INSTDIR"

    ; Stop any running agent so upgrades can replace aems-agent.exe instead
    ; of failing with "Error opening file for writing".
    DetailPrint "Stopping running AEMS Agent instances..."
    nsExec::ExecToLog 'cmd /c taskkill /IM aems-agent.exe /T /F >nul 2>&1'
    Sleep 1000

    ; Copy all files from PyInstaller dist
    File /r "${DIST_DIR}\*.*"

    ; Create Start Menu shortcuts
    CreateDirectory "$SMPROGRAMS\AEMS Agent"
    CreateShortCut "$SMPROGRAMS\AEMS Agent\AEMS Agent.lnk" "$INSTDIR\aems-agent.exe" "run --tray"
    CreateShortCut "$SMPROGRAMS\AEMS Agent\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; Optional: Run on startup
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" \
        "AEMS Agent" '"$INSTDIR\aems-agent.exe" run --tray'

    ; Write uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Registry entries for Add/Remove Programs
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "DisplayName" "AEMS Agent"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "Publisher" "AEMS Project"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "DisplayVersion" "${AGENT_VERSION}"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent" \
        "NoRepair" 1

    ; Save install dir
    WriteRegStr HKCU "Software\AEMS Agent" "InstallDir" "$INSTDIR"

    ; Register aems-agent:// custom URI protocol (per-user)
    WriteRegStr HKCU "Software\Classes\aems-agent" "" "URL:AEMS Agent Launch Protocol"
    WriteRegStr HKCU "Software\Classes\aems-agent" "URL Protocol" ""
    WriteRegStr HKCU "Software\Classes\aems-agent\DefaultIcon" "" '"$INSTDIR\aems-agent.exe",1'
    WriteRegStr HKCU "Software\Classes\aems-agent\shell\open\command" "" \
        '"$INSTDIR\aems-agent.exe" run --tray --launch-from-uri "%1"'
SectionEnd

; Uninstaller section
Section "Uninstall"
    ; Best-effort stop before removing the installed files.
    DetailPrint "Stopping running AEMS Agent instances..."
    nsExec::ExecToLog 'cmd /c taskkill /IM aems-agent.exe /T /F >nul 2>&1'
    Sleep 1000

    ; Remove startup entry
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "AEMS Agent"

    ; Remove Start Menu
    RMDir /r "$SMPROGRAMS\AEMS Agent"

    ; Remove files
    RMDir /r "$INSTDIR"

    ; Remove registry entries
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AEMS Agent"
    DeleteRegKey HKCU "Software\AEMS Agent"
    DeleteRegKey HKCU "Software\Classes\aems-agent"
SectionEnd

Function OpenAemsSettings
    ExecShell "open" "https://api.aems.app/settings/#privacy"
FunctionEnd
