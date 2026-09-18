; ---------------------------------------------------------------------------
; ZkPushAgent-Setup -- installer agen push ZKTeco C3 (Windows).
;
; Kompilasi lewat build_installer.ps1 (butuh Inno Setup 6 + payload di .cache\),
; jangan dikompilasi langsung tanpa menyiapkan isi .cache\.
;
; Yang dilakukan installer ini:
;   1. menyalin SEMUA pl*.dll dari folder SDK yang dipilih ke {app}\sdk
;      (working directory agen = folder itu, jadi jebakan PullLastError=-201
;      tidak mungkin terjadi karena DLL transport selalu bersebelahan),
;   2. memasang Python 3.13 embeddable 32-bit yang dibundel (tanpa menyentuh
;      Python 64-bit milik mesin),
;   3. memasang VC++ Redistributable x86 kalau belum ada (prasyarat DLL),
;   4. menulis environment variable tingkat mesin,
;   5. mendaftarkan task Task Scheduler "ZkPushAgent" (SYSTEM, mulai saat boot),
;   6. memulai agen dan MEMBUKTIKAN jalur HTTP-nya lewat GET /health,
;   7. menulis laporan lengkap ke {app}\CHECK-REPORT.txt.
;
; Installer ini TIDAK menyentuh panel: pemeriksaannya baca-saja (opsional
; membaca nomor seri satu panel yang alamatnya diisi operator).
; ---------------------------------------------------------------------------
#define AppName "ZKTeco Push Agent"
#define AppShortName "ZkPushAgent"
#define AppPublisher "RSMPP (pengganti ZKAccess)"
#ifndef AppVer
  #define AppVer "dev"
#endif
#ifndef VerNum
  #define VerNum "0.0.0.0"
#endif
#define AgentDir AddBackslash(SourcePath) + ".."
#define PayloadDir AddBackslash(SourcePath) + "app"
#define CacheDir AddBackslash(SourcePath) + ".cache"
#define PyPayload AddBackslash(CacheDir) + "python"
#define VcRedistExe AddBackslash(CacheDir) + "vc_redist.x86.exe"
#define EnvKeyName "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

[Setup]
AppId={{8F3A2C1E-6B41-4E7A-9C2D-5A17B0E4D3F1}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=admin
MinVersion=6.3
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
SetupLogging=yes
OutputDir=dist
OutputBaseFilename=ZkPushAgent-Setup
UninstallDisplayName={#AppName} ({#AppVer})
VersionInfoVersion={#VerNum}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#AgentDir}\zk_push_agent.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AgentDir}\check_setup.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AgentDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\run_agent.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\install_task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\check_agent.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\gen_token.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PyPayload}\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#VcRedistExe}"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Periksa agen (DLL, task, /health)"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -NoExit -File ""{app}\check_agent.ps1"""; WorkingDir: "{app}"; Comment: "Jalankan pemeriksaan lengkap agen push"
Name: "{group}\Lihat laporan pemeriksaan"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\CHECK-REPORT.txt"""
Name: "{group}\Lihat log agen"; Filename: "{sys}\notepad.exe"; Parameters: """{commonappdata}\ZkPushAgent\agent.log"""
Name: "{group}\Buka Task Scheduler (task ZkPushAgent)"; Filename: "{sys}\taskschd.msc"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\sdk"
Type: filesandordirs; Name: "{app}\python"
Type: files; Name: "{app}\CHECK-REPORT.txt"
Type: files; Name: "{app}\BACKEND-SNIPPET.txt"
Type: dirifempty; Name: "{app}"

[Code]
const
  EnvKey = '{#EnvKeyName}';
  TaskName = 'ZkPushAgent';
  FirewallRule = 'ZkPushAgent';
  SafeHost = '127.0.0.1';
  DefaultPort = '8081';
  DefaultBuffer = '65536';
  IdxToken = 0;
  IdxPort = 1;
  IdxBind = 2;
  IdxPanelIp = 3;
  IdxPanelPwd = 4;

var
  SdkPage: TInputDirWizardPage;
  CfgPage: TInputQueryWizardPage;
  OpenReportCheckBox: TNewCheckBox;
  Token: String;
  TokenFromParam: Boolean;
  BindHost: String;
  AgentPort: String;
  BufferSize: String;
  PanelIp: String;
  PanelPwd: String;
  SdkFolder: String;
  DllTargetPath: String;
  ReportFile: String;
  PostProblems: TStringList;
  VerdictLine: String;
  RebootNeeded: Boolean;
  ExtraNote: String;

{ ------------------------------------------------------------------ helpers }
procedure AddProblem(const Text: String);
begin
  PostProblems.Add(Text);
end;

procedure SetStatus(const Text: String);
begin
  if WizardForm.StatusLabel <> nil then
    WizardForm.StatusLabel.Caption := Text;
end;

function FindSdkFolder(): String;
var
  Candidates: array[0..7] of String;
  I: Integer;
  FindRec: TFindRec;
  Sub: String;
  SdRoot: String;
  PreviousDll: String;
begin
  Result := '';
  SdRoot := AddBackslash(ExpandConstant('{sd}'));
  Candidates[0] := ExpandConstant('{sd}\PullSDK');
  Candidates[1] := ExpandConstant('{sd}\ZKTeco\PullSDK');
  Candidates[2] := ExpandConstant('{sd}\ZKAccess');
  Candidates[3] := ExpandConstant('{pf32}\ZKTeco');
  Candidates[4] := ExpandConstant('{pf32}\ZKAccess');
  Candidates[5] := ExpandConstant('{localappdata}\Programs\Python');
  Candidates[6] := ExpandConstant('{src}');
  Candidates[7] := '';

  { Kalau pernah dipasang, SDK sudah ada di folder aplikasi; ambil lokasinya
    dari ZK_PULLSDK_DLL. Jangan pakai konstanta app di sini: selama
    InitializeWizard konstanta itu belum punya nilai (halaman folder belum
    dibuat) dan Inno melempar "attempt to expand the app constant before it was
    initialized" - installer mati sebelum menampilkan halaman pertama. }
  if RegQueryStringValue(HKLM, EnvKey, 'ZK_PULLSDK_DLL', PreviousDll) then
    Candidates[6] := ExtractFileDir(PreviousDll);

  for I := 0 to 7 do
    if (Candidates[I] <> '') and FileExists(AddBackslash(Candidates[I]) + 'plcommpro.dll') then
    begin
      Result := Candidates[I];
      Exit;
    end;

  { Satu tingkat di bawah root drive C: saja, supaya wizard tidak menggantung
    memindai seluruh disk. }
  if FindFirst(SdRoot + '*', FindRec) then
  begin
    try
      repeat
        if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0)
           and (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Sub := SdRoot + FindRec.Name;
          if FileExists(AddBackslash(Sub) + 'plcommpro.dll') then
          begin
            Result := Sub;
            Exit;
          end;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function MissingTransportDlls(const Folder: String): String;
var
  Missing: String;
  Base: String;
begin
  Base := AddBackslash(Folder);
  Missing := '';
  if not FileExists(Base + 'plcomms.dll') then Missing := Missing + 'plcomms.dll ';
  if not FileExists(Base + 'plrscagent.dll') then Missing := Missing + 'plrscagent.dll ';
  if not FileExists(Base + 'plrscomm.dll') then Missing := Missing + 'plrscomm.dll ';
  if not FileExists(Base + 'pltcpcomm.dll') then Missing := Missing + 'pltcpcomm.dll ';
  if not FileExists(Base + 'plusbcomm.dll') then Missing := Missing + 'plusbcomm.dll ';
  Result := Trim(Missing);
end;

procedure CopyFolder(const Src, Dst: String);
var
  FindRec: TFindRec;
  SrcPath, DstPath, Name: String;
begin
  if not DirExists(Dst) then
    ForceDirectories(Dst);
  if not FindFirst(AddBackslash(Src) + '*', FindRec) then
    Exit;
  try
    repeat
      Name := FindRec.Name;
      if (Name <> '.') and (Name <> '..') then
      begin
        SrcPath := AddBackslash(Src) + Name;
        DstPath := AddBackslash(Dst) + Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          CopyFolder(SrcPath, DstPath)
        else
          CopyFile(SrcPath, DstPath, False);
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure SetEnvValue(const Name, Value: String);
begin
  if Value = '' then
    RegDeleteValue(HKLM, EnvKey, Name)
  else
    RegWriteStringValue(HKLM, EnvKey, Name, Value);
end;

function NeedVcRedist(): Boolean;
begin
  Result := (not FileExists(ExpandConstant('{syswow64}\vcruntime140.dll')))
         or (not FileExists(ExpandConstant('{syswow64}\vcruntime140_1.dll')))
         or (not FileExists(ExpandConstant('{syswow64}\msvcp140.dll')));
end;

function RunPowerShell(const Params: String; var ExitCode: Integer): Boolean;
begin
  Result := Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass ' + Params,
                 ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);
end;

{ Token yang kuat: dibuat PowerShell (RandomNumberGenerator), bukan Random()
  bawaan Pascal. Tanpa ini, token hanya 32-bit entropi yang bisa ditebak. }
function GenerateToken(): String;
var
  ExitCode: Integer;
  OutFile: String;
  Lines: TArrayOfString;
begin
  Result := '';
  OutFile := ExpandConstant('{tmp}\zktoken.txt');
  DeleteFile(OutFile);
  RunPowerShell('-File "' + ExpandConstant('{app}\gen_token.ps1') + '" -OutFile "' + OutFile + '"', ExitCode);
  if LoadStringsFromFile(OutFile, Lines) and (GetArrayLength(Lines) > 0) then
    Result := Trim(Lines[0]);
end;

procedure SyncFromWizard();
begin
  if SdkPage.Values[0] <> '' then
    SdkFolder := RemoveBackslashUnlessRoot(Trim(SdkPage.Values[0]));
  if SdkFolder = '' then
    SdkFolder := ExpandConstant('{sd}\PullSDK');

  Token := Trim(CfgPage.Values[IdxToken]);
  AgentPort := Trim(CfgPage.Values[IdxPort]);
  BindHost := Trim(CfgPage.Values[IdxBind]);
  PanelIp := Trim(CfgPage.Values[IdxPanelIp]);
  PanelPwd := CfgPage.Values[IdxPanelPwd];

  if AgentPort = '' then AgentPort := DefaultPort;
  if BindHost = '' then BindHost := SafeHost;
  if BufferSize = '' then BufferSize := DefaultBuffer;
end;

{ ------------------------------------------------------------- wizard setup }
procedure InitializeWizard();
var
  Old: String;
begin
  PostProblems := TStringList.Create;
  VerdictLine := '';
  ExtraNote := '';
  RebootNeeded := False;
  TokenFromParam := False;
  BufferSize := '';

  { Nilai dari parameter command line (untuk pemasangan senyap / massal). }
  SdkFolder := ExpandConstant('{param:SDK|}');
  Token := ExpandConstant('{param:TOKEN|}');
  BindHost := ExpandConstant('{param:HOST|}');
  AgentPort := ExpandConstant('{param:PORT|}');
  BufferSize := ExpandConstant('{param:BUFFER|}');
  PanelIp := ExpandConstant('{param:PANELIP|}');
  PanelPwd := ExpandConstant('{param:PANELPWD|}');

  { Kalau tidak ada parameter, pakai nilai dari pemasangan sebelumnya supaya
    upgrade tidak diam-diam mengganti token (backend akan langsung 401). }
  if Token = '' then
  begin
    if RegQueryStringValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_TOKEN', Old) then Token := Old;
  end
  else
    TokenFromParam := True;
  if BindHost = '' then
  begin
    if not RegQueryStringValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_HOST', Old) then Old := SafeHost;
    BindHost := Old;
  end;
  if AgentPort = '' then
  begin
    if not RegQueryStringValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_PORT', Old) then Old := DefaultPort;
    AgentPort := Old;
  end;
  if BufferSize = '' then
  begin
    if not RegQueryStringValue(HKLM, EnvKey, 'ZK_AGENT_BUFFER_SIZE', Old) then Old := DefaultBuffer;
    BufferSize := Old;
  end;
  if PanelPwd = '' then
    RegQueryStringValue(HKLM, EnvKey, 'ZK_PANEL_PASSWORD', PanelPwd);

  if SdkFolder = '' then
    SdkFolder := FindSdkFolder();
  if SdkFolder = '' then
    SdkFolder := ExpandConstant('{sd}\PullSDK');

  SdkPage := CreateInputDirPage(wpSelectDir,
    'SDK resmi ZKTeco (plcommpro.dll)',
    'Installer perlu folder yang berisi SEMUA pl*.dll dari SDK.',
    'Seluruh berkas pl*.dll akan disalin ke folder aplikasi, sehingga DLL transport ' +
    'selalu bersebelahan dengan plcommpro.dll. Itu yang mencegah Connect() gagal ' +
    'dengan PullLastError=-201. SDK tidak bisa diunduh otomatis (situs ZKTeco ' +
    'berbasis JavaScript), jadi siapkan berkasnya lebih dulu.',
    False, SdkFolder);
  SdkPage.Add('Folder SDK ZKTeco:');

  CfgPage := CreateInputQueryPage(SdkPage.ID,
    'Konfigurasi agen',
    'Token, port, dan host bind',
    'Token dipakai backend untuk membuktikan identitasnya. Kosongkan supaya installer ' +
    'membuatkan token acak 30 byte; token hasilnya ditampilkan di halaman terakhir dan ' +
    'disimpan di CHECK-REPORT.txt serta BACKEND-SNIPPET.txt.');
  CfgPage.Add('Token (kosongkan = buat otomatis):', False);
  CfgPage.Add('Port:', False);
  CfgPage.Add('Bind host (127.0.0.1 = hanya mesin ini, 0.0.0.0 = dari mesin lain):', False);
  CfgPage.Add('IP satu panel untuk uji koneksi baca-saja (opsional):', False);
  CfgPage.Add('Password panel (opsional, kalau panel memerlukannya):', True);
  CfgPage.Values[IdxToken] := Token;
  CfgPage.Values[IdxPort] := AgentPort;
  CfgPage.Values[IdxBind] := BindHost;
  CfgPage.Values[IdxPanelIp] := PanelIp;
  CfgPage.Values[IdxPanelPwd] := PanelPwd;

  OpenReportCheckBox := TNewCheckBox.Create(WizardForm);
  OpenReportCheckBox.Parent := WizardForm.FinishedPage;
  OpenReportCheckBox.Left := WizardForm.FinishedLabel.Left;
  OpenReportCheckBox.Top := WizardForm.FinishedLabel.Top + ScaleY(80);
  OpenReportCheckBox.Width := WizardForm.FinishedPage.Width - OpenReportCheckBox.Left - ScaleX(8);
  OpenReportCheckBox.Caption := 'Buka laporan pemeriksaan (CHECK-REPORT.txt)';
  OpenReportCheckBox.Visible := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if WizardSilent then
  begin
    if (PageID = SdkPage.ID) or (PageID = CfgPage.ID) then
      Result := True;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Missing: String;
begin
  Result := True;

  if CurPageID = SdkPage.ID then
  begin
    SdkFolder := RemoveBackslashUnlessRoot(Trim(SdkPage.Values[0]));
    if not FileExists(AddBackslash(SdkFolder) + 'plcommpro.dll') then
    begin
      MsgBox('plcommpro.dll tidak ditemukan di:' + #13#10 + SdkFolder + #13#10#13#10 +
             'Pilih folder yang berisi SEMUA pl*.dll dari SDK resmi ZKTeco ' +
             '(mis. C:\PullSDK).', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    Missing := MissingTransportDlls(SdkFolder);
    if Missing <> '' then
    begin
      if MsgBox('Perhatian: DLL transport ini tidak ada di folder tersebut:' + #13#10 +
                '  ' + Missing + #13#10#13#10 +
                'Tanpa DLL itu Connect() ke panel akan gagal dengan ' +
                'PullLastError=-201.' + #13#10#13#10 + 'Lanjutkan tetap?',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;

  if CurPageID = CfgPage.ID then
  begin
    if Trim(CfgPage.Values[IdxPort]) <> '' then
    begin
      if (StrToIntDef(Trim(CfgPage.Values[IdxPort]), 0) < 1024)
         or (StrToIntDef(Trim(CfgPage.Values[IdxPort]), 0) > 65535) then
      begin
        MsgBox('Port harus angka antara 1024 dan 65535.', mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;
    if (Trim(CfgPage.Values[IdxBind]) <> '') and (Trim(CfgPage.Values[IdxBind]) <> SafeHost)
       and (Trim(CfgPage.Values[IdxBind]) <> '0.0.0.0') then
    begin
      MsgBox('Bind host hanya boleh ' + SafeHost + ' (hanya mesin ini) atau 0.0.0.0 ' +
             '(bisa diakses mesin lain).', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Trim(CfgPage.Values[IdxPanelIp]) <> '' then
    begin
      if MissingTransportDlls(SdkFolder) <> '' then
      begin
        MsgBox('Uji koneksi ke panel dilewati: DLL transport belum lengkap di folder SDK ' +
               '(risiko PullLastError=-201).', mbInformation, MB_OK);
        CfgPage.Values[IdxPanelIp] := '';
      end;
    end;
  end;

  if Result and (CurPageID = wpReady) then
    SyncFromWizard();
end;

function UpdateReadyMemo(const Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  SyncFromWizard();
  Result :=
    'Folder aplikasi:' + NewLine + '  ' + ExpandConstant('{app}') + NewLine + NewLine +
    'Folder SDK (akan disalin ke aplikasi):' + NewLine + '  ' + SdkFolder + NewLine + NewLine +
    'Port / bind host:' + NewLine + '  ' + AgentPort + ' / ' + BindHost;
  if BindHost <> SafeHost then
    Result := Result + ' (firewall dibuka untuk jaringan lokal)';
  Result := Result + NewLine + NewLine +
    'Token:' + NewLine + '  ';
  if Token = '' then
    Result := Result + '(dibuat otomatis saat pemasangan)'
  else
    Result := Result + Token;
  Result := Result + NewLine + NewLine +
    'Setelah pemasangan:' + NewLine +
    '  - agen didaftarkan sebagai task Task Scheduler "ZkPushAgent" (akun SYSTEM, mulai saat boot)' + NewLine +
    '  - agen dinyalakan lalu dibuktikan lewat GET /health (tanpa menulis ke panel)' + NewLine +
    '  - laporan pemeriksaan ditulis ke CHECK-REPORT.txt di folder aplikasi' + NewLine + NewLine +
    'Perkiraan waktu 1-2 menit (VC++ Runtime x86 dipasang kalau belum ada).';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  SyncFromWizard();
  if not FileExists(AddBackslash(SdkFolder) + 'plcommpro.dll') then
  begin
    Result := 'plcommpro.dll tidak ditemukan di ' + SdkFolder + '.' + #13#10 +
              'Taruh semua pl*.dll dari SDK resmi ZKTeco di satu folder, lalu jalankan ' +
              'installer lagi (atau pakai /SDK="C:\PullSDK" saat pemasangan senyap).';
    Exit;
  end;
  { Token kosong = dibuat setelah berkas terpasang (butuh gen_token.ps1). }
  NeedsRestart := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
  SdkTarget: String;
  Missing: String;
  Lines: TArrayOfString;
  Snippet: TArrayOfString;
  I: Integer;
  Pwsh: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  SdkTarget := ExpandConstant('{app}\sdk');
  DllTargetPath := SdkTarget + '\plcommpro.dll';
  ReportFile := ExpandConstant('{app}\CHECK-REPORT.txt');

  { 1. VC++ Redistributable x86: prasyarat memuat plcommpro.dll. Pesan
       "Could not find module ... or one of its dependencies" hampir selalu
       berarti ini, bukan path DLL yang salah. }
  if NeedVcRedist() then
  begin
    SetStatus('Memasang Microsoft Visual C++ Redistributable (x86)...');
    if Exec(ExpandConstant('{tmp}\vc_redist.x86.exe'), '/install /quiet /norestart', '',
            SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    begin
      if ExitCode = 3010 then
        RebootNeeded := True;
      if (ExitCode <> 0) and (ExitCode <> 3010) and (ExitCode <> 1638) then
        AddProblem('VC++ Redistributable x86 gagal dipasang (kode ' + IntToStr(ExitCode) + ')');
    end
    else
      AddProblem('VC++ Redistributable x86 tidak bisa dijalankan');
  end;

  { 2. DLL SDK -> folder sdk di dalam folder aplikasi }
  SetStatus('Menyalin DLL SDK ZKTeco...');
  CopyFolder(SdkFolder, SdkTarget);
  if not FileExists(DllTargetPath) then
    AddProblem('plcommpro.dll tidak tersalin ke ' + SdkTarget);
  Missing := MissingTransportDlls(SdkTarget);
  if Missing <> '' then
    AddProblem('DLL transport belum ada di ' + SdkTarget + ': ' + Missing);

  { 3. Token (kalau operator mengosongkan kolomnya). }
  if Token = '' then
  begin
    Token := GenerateToken();
    if Token = '' then
      AddProblem('token otomatis gagal dibuat - isi token manual lalu pasang ulang');
  end;

  { 4. Environment variable tingkat mesin. Ini kontrak agen: nama variabelnya
       dibaca langsung oleh zk_push_agent.py (lihat tests/test_agent_installer.py). }
  SetStatus('Menulis environment variable...');
  SetEnvValue('ZK_PUSH_AGENT_TOKEN', Token);
  SetEnvValue('ZK_PUSH_AGENT_HOST', BindHost);
  SetEnvValue('ZK_PUSH_AGENT_PORT', AgentPort);
  SetEnvValue('ZK_PULLSDK_DLL', DllTargetPath);
  SetEnvValue('ZK_AGENT_BUFFER_SIZE', BufferSize);
  SetEnvValue('ZK_PANEL_PASSWORD', PanelPwd);

  { 5. Berkas contekan untuk operator: baris yang harus ditempel ke env backend. }
  SetArrayLength(Snippet, 6);
  Snippet[0] := '# Tempel ke environment SERVER BACKEND, lalu restart backend:';
  Snippet[1] := 'PUSH_AGENT_URL=http://' + GetComputerNameString() + ':' + AgentPort;
  Snippet[2] := 'PUSH_AGENT_TOKEN=' + Token;
  Snippet[3] := '';
  Snippet[4] := '# Kalau backend jalan di mesin ini juga, pakai ' + SafeHost + ':' + AgentPort;
  Snippet[5] := '# Verifikasi menyeluruh: powershell -ExecutionPolicy Bypass -File "' +
                ExpandConstant('{app}\check_agent.ps1') + '"';
  SaveStringsToUTF8FileWithoutBOM(ExpandConstant('{app}\BACKEND-SNIPPET.txt'), Snippet, False);

  { 6. Task Scheduler }
  SetStatus('Mendaftarkan task ' + TaskName + '...');
  Pwsh := '-File "' + ExpandConstant('{app}\install_task.ps1') + '"' +
          ' -Python "' + ExpandConstant('{app}\python\python.exe') + '"' +
          ' -Launcher "' + ExpandConstant('{app}\run_agent.cmd') + '"' +
          ' -TaskName "' + TaskName + '"';
  if (not RunPowerShell(Pwsh, ExitCode)) or (ExitCode <> 0) then
    AddProblem('pendaftaran task gagal (kode ' + IntToStr(ExitCode) + '). Jalankan manual: ' +
               'powershell -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install_task.ps1') + '" -Python "' +
               ExpandConstant('{app}\python\python.exe') + '"');

  { 7. Firewall: hanya kalau agen memang harus dijangkau dari mesin lain.
       remoteip=localsubnet supaya tidak terbuka ke seluruh jaringan. }
  if BindHost <> SafeHost then
  begin
    SetStatus('Membuka firewall untuk port ' + AgentPort + '...');
    Exec('netsh.exe', 'advfirewall firewall delete rule name="' + FirewallRule + '"', '',
         SW_HIDE, ewWaitUntilTerminated, ExitCode);
    if (not Exec('netsh.exe', 'advfirewall firewall add rule name="' + FirewallRule +
                 '" dir=in action=allow protocol=TCP localport=' + AgentPort +
                 ' program="' + ExpandConstant('{app}\python\python.exe') +
                 '" remoteip=localsubnet enable=yes profile=any', '',
                 SW_HIDE, ewWaitUntilTerminated, ExitCode)) or (ExitCode <> 0) then
      AddProblem('aturan firewall gagal dibuat - backend di mesin lain tidak akan bisa menghubungi port ' +
                 AgentPort);
  end;

  { 8. Nyalakan agen dan buktikan /health menjawab. }
  SetStatus('Menjalankan agen dan memeriksa /health...');
  Pwsh := '-File "' + ExpandConstant('{app}\check_agent.ps1') + '"' +
          ' -Python "' + ExpandConstant('{app}\python\python.exe') + '"' +
          ' -AgentDir "' + ExpandConstant('{app}') + '"' +
          ' -Dll "' + DllTargetPath + '"' +
          ' -Token "' + Token + '" -AgentHost "' + BindHost + '" -Port "' + AgentPort + '"' +
          ' -TaskName "' + TaskName + '" -Report "' + ReportFile + '" -Restart';
  if PanelIp <> '' then
    Pwsh := Pwsh + ' -PanelIp "' + PanelIp + '"';
  RunPowerShell(Pwsh, ExitCode);
  if ExitCode <> 0 then
    AddProblem('pemeriksaan akhir melaporkan masalah - baca CHECK-REPORT.txt');

  { 9. Verdict untuk halaman terakhir. }
  if FileExists(ReportFile) and LoadStringsFromFile(ReportFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Copy(Lines[I], 1, 8) = 'VERDICT:' then
        VerdictLine := Lines[I];

  if VerdictLine = '' then
    VerdictLine := 'VERDICT: PERIKSA - laporan pemeriksaan tidak terbaca';

  if PostProblems.Count > 0 then
  begin
    ExtraNote := 'Catatan tambahan dari installer:';
    for I := 0 to PostProblems.Count - 1 do
      ExtraNote := ExtraNote + #13#10 + '  - ' + PostProblems[I];
  end;
end;

function CountLines(const Text: String): Integer;
var
  I: Integer;
begin
  Result := 1;
  for I := 1 to Length(Text) do
    if Copy(Text, I, 2) = #13#10 then
      Result := Result + 1;
end;

{ Label halaman akhir: teksnya panjang (verdict + token + path), jadi tingginya
  dihitung sendiri. Kalau tidak, teks terpotong karena FinishedLabel pada gaya
  wizard modern tidak tumbuh sendiri. }
procedure SetFinishedText(const Text: String);
begin
  WizardForm.FinishedLabel.AutoSize := False;
  WizardForm.FinishedLabel.WordWrap := True;
  WizardForm.FinishedLabel.Width := WizardForm.FinishedPage.Width -
    WizardForm.FinishedLabel.Left - ScaleX(8);
  WizardForm.FinishedLabel.Caption := Text;
  WizardForm.FinishedLabel.Height := ScaleY(CountLines(Text) * 22 + 16);
  OpenReportCheckBox.Left := WizardForm.FinishedLabel.Left;
  OpenReportCheckBox.Top := WizardForm.FinishedLabel.Top +
    WizardForm.FinishedLabel.Height + ScaleY(6);
  OpenReportCheckBox.Width := WizardForm.FinishedLabel.Width;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Text: String;
begin
  if CurPageID <> wpFinished then
    Exit;

  Text :=
    'Agen terpasang sebagai task Task Scheduler "' + TaskName + '" (akun SYSTEM, mulai saat boot).' + #13#10#13#10 +
    VerdictLine + #13#10 +
    'Token: ' + Token + '    Port: ' + AgentPort + ' (bind ' + BindHost + ')' + #13#10 +
    'Laporan: ' + ReportFile;

  if ExtraNote <> '' then
    Text := Text + #13#10#13#10 + ExtraNote;

  if RebootNeeded then
    Text := Text + #13#10#13#10 +
      'VC++ Runtime baru dipasang: Windows perlu restart dahulu sebelum agen bisa memuat DLL.';

  SetFinishedText(Text);

  if FileExists(ReportFile) then
  begin
    OpenReportCheckBox.Visible := True;
    { Dialog TIDAK boleh muncul saat pemasangan senyap. Dengan /VERYSILENT tidak
      ada yang bisa menekan OK, jadi pemasangan massal akan menggantung di setiap
      PC begitu verdictnya bukan OK. Detailnya tetap lengkap di CHECK-REPORT.txt. }
    if (not WizardSilent) and (Copy(VerdictLine, 1, 15) <> 'VERDICT: OK - ag') then
      MsgBox('Pemeriksaan akhir belum sepenuhnya OK.' + #13#10#13#10 +
             VerdictLine + #13#10 + 'Laporan lengkap: ' + ReportFile + #13#10 +
             'Log agen: ' + ExpandConstant('{commonappdata}\ZkPushAgent\agent.log'),
             mbError, MB_OK);
  end;
end;

procedure DeinitializeSetup();
var
  ErrCode: Integer;
begin
  { Halaman terakhir: buka laporan kalau operator mencentangnya. }
  if OpenReportCheckBox.Checked and FileExists(ReportFile) then
  begin
    ErrCode := 0;
    ShellExec('open', ReportFile, '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
  end;
  PostProblems.Free;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExitCode: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  Exec('schtasks.exe', '/end /tn ' + TaskName, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  Exec('schtasks.exe', '/delete /tn ' + TaskName + ' /f', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  Exec('netsh.exe', 'advfirewall firewall delete rule name="' + FirewallRule + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ExitCode);

  { Environment variable machine-wide dibersihkan supaya agen tidak pernah
    start dengan token basi. }
  RegDeleteValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_TOKEN');
  RegDeleteValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_HOST');
  RegDeleteValue(HKLM, EnvKey, 'ZK_PUSH_AGENT_PORT');
  RegDeleteValue(HKLM, EnvKey, 'ZK_PULLSDK_DLL');
  RegDeleteValue(HKLM, EnvKey, 'ZK_AGENT_BUFFER_SIZE');
  RegDeleteValue(HKLM, EnvKey, 'ZK_PANEL_PASSWORD');
end;
