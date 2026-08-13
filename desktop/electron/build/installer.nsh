; LeAgent NSIS custom installer hooks
; Included by electron-builder via nsis.include in electron-builder.yml
;
; NOTE: Do NOT !include "MUI2.nsh" here — electron-builder's own NSIS
; scaffolding already includes it, and a second include causes conflicts.
; Likewise, do NOT insert MUI_PAGE_DIRECTORY inside customInstallMode;
; electron-builder's assisted installer adds that page automatically when
; allowToChangeInstallationDirectory is true in electron-builder.yml.

; ── Custom pages ──

!macro customInit
  ; ``initMultiUser`` restores InstallLocation before this hook. Only select
  ; our legacy per-user default on a fresh install; overwriting $INSTDIR here
  ; would move upgrades away from a user-selected location.
  ; A caller's explicit `/D=...` must win as well. This is needed for
  ; unattended deployment and prevents the default below from silently
  ; redirecting an install to a different location.
  ${GetParameters} $R0
  ${GetOptions} $R0 "/D=" $R1
  ${If} ${Errors}
    ${if} $hasPerUserInstallation == "0"
    ${andif} $hasPerMachineInstallation == "0"
      StrCpy $INSTDIR "$LOCALAPPDATA\LeAgent"
    ${endif}
  ${EndIf}
!macroend

; ── Uninstall prompt for user data ──

!macro customUnInit
  ; Nothing extra on uninstall init
!macroend

!macro customRemoveFiles
  ; App files: replicate electron-builder's default removal
  ; (atomic move on update so a locked file can abort + restore)
  ${if} ${isUpdated}
    CreateDirectory "$PLUGINSDIR\old-install"
    Push ""
    Call un.atomicRMDir
    Pop $R0
    ${if} $R0 != 0
      DetailPrint "File is busy, aborting: $R0"
      Push ""
      Call un.restoreFiles
      Pop $R0
      Abort `Can't rename "$INSTDIR" to "$PLUGINSDIR\old-install".`
    ${endif}
  ${endif}
  SetOutPath $TEMP
  RMDir /r $INSTDIR

  ; User data: never removed on updates or silent uninstalls (/SD IDNO);
  ; only an interactive full uninstall may ask.
  ${ifNot} ${isUpdated}
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "Remove LeAgent user data (settings, databases, logs)?$\n$\nUser data location: $APPDATA\LeAgent" \
      /SD IDNO IDNO skip_data_removal
    RMDir /r "$APPDATA\LeAgent"
    skip_data_removal:
  ${endif}
!macroend
