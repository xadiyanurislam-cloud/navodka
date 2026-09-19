; Установщик Inno Setup.
;
; Ставим в папку пользователя (LocalAppData), а не в Program Files:
; установка в Program Files требует прав администратора, и на рабочих
; машинах в компаниях их обычно нет — установщик просто не запустится.

#define AppName "Наводка"
#define AppExe  "Navodka.exe"

; Версию передаёт сборка: ISCC /DAppVersion=0.3.0. Значение ниже —
; запасное, для ручной сборки. Раньше номер был вписан здесь руками и
; успел разойтись с app/settings.py — из-за этого обновление считало
; свежую сборку старой.
#ifndef AppVersion
  #define AppVersion "0.13.0"
#endif

[Setup]
AppId={{9E7C2C41-7B2E-4E5E-9C1B-3B6A0F2D5A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Navodka
DefaultDirName={localappdata}\Navodka
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Navodka-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
; Обновление запускается из самой программы, и к моменту установки её exe
; ещё может быть занят. Менеджер перезапуска Windows закрывает её сам,
; иначе установщик упирается в «файл используется».
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
; Тот же AppId ставится поверх прежней установки, без второй копии в
; списке программ.
UsePreviousAppDir=yes
DisableDirPage=auto

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
; Галочка отмечена по умолчанию: программу открывают каждый день, и
; искать её в меню «Пуск» каждый раз никто не станет.
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; \
  GroupDescription: "Дополнительно:"

[Files]
Source: "dist\Navodka\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "icon.ico";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";         Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";   Filename: "{app}\{#AppExe}"; \
  IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; \
  Flags: nowait postinstall skipifsilent
; Тихая установка из самой программы: окно закрылось по нашей команде,
; и не открыть новую версию обратно — значит оставить человека перед
; пустым столом и мыслью, что программа пропала.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchAfterUpdate

; База лежит в LocalAppData\navodka и при удалении программы остаётся:
; в ней результаты работы, и стирать их вместе с exe нельзя.
[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; [Code] обязан быть последней секцией скрипта, и имена здесь только
; латиницей: Pascal Script в Inno не разбирает кириллицу в идентификаторах.
[Code]
function RelaunchAfterUpdate(): Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
