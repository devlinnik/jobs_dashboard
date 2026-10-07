// Standalone .NET Framework 4.8 launcher; C# 5 for the Windows built-in compiler.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
[assembly: AssemblyTitle("Devlin Desk Launcher")]
[assembly: AssemblyVersion("1.10.0.0")]
[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]
namespace DevlinInstaller {
    internal static class Program {
        internal static readonly string[] Resources = { "install-windows.ps1", "package_windows.py", "check-running.ps1", "refresh-icons.ps1", "devlin-desk.ico" };
        internal static string Quote(string value) {
            // CommandLineToArgvW quoting, including trailing backslashes and literal quotes.
            var result = new StringBuilder("\""); int slashes = 0;
            foreach (char c in value) {
                if (c == '\\') { slashes++; continue; }
                result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
                result.Append(c); slashes = 0;
            }
            result.Append('\\', slashes * 2); return result.Append('"').ToString();
        }
        internal static string PowerShell { get { return Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe"); } }
        internal static void Extract(string folder) {
            foreach (string name in Resources) using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream("DevlinInstaller." + name)) {
                if (input == null) throw new InvalidDataException("Missing embedded installer: " + name);
                using (var output = File.Create(Path.Combine(folder, name))) input.CopyTo(output);
            }
        }
        internal static string[] Locate(string origin) {
            string folder = Path.GetDirectoryName(Path.GetFullPath(origin));
            string fallback = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "DevlinDeskUpdater");
            if (File.Exists(Path.Combine(folder, "run.py"))) return new[] { folder, Path.Combine(folder, @"dist\DevlinDesk") };
            string manifest = Path.Combine(folder, @"maintenance\install-info.json");
            if (File.Exists(manifest)) {
                var data = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(manifest, Encoding.UTF8));
                object source; string path = data.TryGetValue("source_directory", out source) ? Convert.ToString(source) : fallback;
                return new[] { String.IsNullOrWhiteSpace(path) ? fallback : Path.GetFullPath(path), folder };
            }
            return new[] { fallback, Path.Combine(fallback, @"dist\DevlinDesk") };
        }
        internal static int Execute(ProcessStartInfo start, Action<string> receive) {
            using (var process = new Process { StartInfo = start }) {
                DataReceivedEventHandler handler = delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) receive(e.Data); };
                process.OutputDataReceived += handler; process.ErrorDataReceived += handler;
                process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
                process.WaitForExit(); return process.ExitCode;
            }
        }
        internal static ProcessStartInfo InstallerProcess(IEnumerable<string> arguments, string directory) {
            return new ProcessStartInfo(PowerShell, String.Join(" ", arguments.Select(Quote))) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8, WorkingDirectory = directory };
        }
        [STAThread] private static int Main(string[] args) {
            if (args.Length == 2 && args[0] == "--self-test-output") return SelfTest(args[1]);
            string temp = null;
            try {
                if (args.Length == 3 && args[0] == "--handoff") {
                    int parent = Int32.Parse(args[1]);
                    try { using (var p = Process.GetProcessById(parent)) if (!p.WaitForExit(15000)) throw new IOException("Launcher handoff timed out."); } catch (ArgumentException) { }
                    string candidate = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                    // Only a folder created by this launcher may be extracted to or removed.
                    if (!Path.GetFileName(candidate).StartsWith("DevlinLauncher-", StringComparison.Ordinal) || !String.Equals(Path.GetDirectoryName(candidate), Path.GetTempPath().TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) throw new IOException("Invalid launcher workspace.");
                    temp = candidate; Extract(temp);
                    var paths = Locate(args[2]);
                    // Use current checkout scripts after git pull; standalone launchers use embedded copies.
                    foreach (string name in Resources.Where(n => n != "devlin-desk.ico")) {
                        string current = Path.Combine(paths[0], "scripts", name);
                        if (File.Exists(Path.Combine(paths[0], "run.py")) && File.Exists(current)) File.Copy(current, Path.Combine(temp, name), true);
                    }
                    Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                    Application.Run(new LauncherForm(temp, paths[0], paths[1]));
                    return 0;
                }
                if (args.Length != 0) throw new ArgumentException("Unsupported launcher arguments.");
                temp = Path.Combine(Path.GetTempPath(), "DevlinLauncher-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(temp);
                string origin = Assembly.GetExecutingAssembly().Location;
                string child = Path.Combine(temp, "DevlinLauncher.exe"); File.Copy(origin, child);
                Process.Start(new ProcessStartInfo(child, "--handoff " + Process.GetCurrentProcess().Id + " " + Quote(origin)) { UseShellExecute = false });
                temp = null; // the child owns cleanup; original executable is now unlocked
                return 0;
            } catch (Exception e) { MessageBox.Show(e.Message, "Devlin Desk — ошибка запуска", MessageBoxButtons.OK, MessageBoxIcon.Error); return 1; }
            finally { if (temp != null) CleanupAfterExit(temp); }
        }
        private static void CleanupAfterExit(string folder) {
            // Wait for this exact process; never terminate processes or touch application data.
            string script = "$ErrorActionPreference='Stop'; try { Wait-Process -Id " + Process.GetCurrentProcess().Id + " -ErrorAction SilentlyContinue } finally { Remove-Item -LiteralPath '" + folder.Replace("'", "''") + "' -Recurse -Force -ErrorAction SilentlyContinue }";
            try { Process.Start(new ProcessStartInfo(PowerShell, "-NoProfile -NonInteractive -EncodedCommand " + Convert.ToBase64String(Encoding.Unicode.GetBytes(script))) { UseShellExecute = false, CreateNoWindow = true }); } catch { }
        }
        private static int SelfTest(string output) {
            string folder = Path.Combine(Path.GetTempPath(), "DevlinLauncher-Test-" + Guid.NewGuid().ToString("N"));
            var checks = new List<string>(); string error = null;
            try {
                Directory.CreateDirectory(folder); Extract(folder);
                foreach (string name in Resources) if (new FileInfo(Path.Combine(folder, name)).Length == 0) throw new Exception("Empty resource " + name);
                using (var icon = new Icon(Path.Combine(folder, "devlin-desk.ico"))) if (icon.Width < 16) throw new Exception("Invalid icon");
                checks.Add("embedded installer and icon");
                if (Quote(@"D:\Проект с пробелами\") != "\"D:\\Проект с пробелами\\\\\"" || Quote("a\"b") != "\"a\\\"b\"") throw new Exception("Argument quoting failed");
                checks.Add("Unicode, spaces, quotes and trailing backslashes");
                File.WriteAllText(Path.Combine(folder, "run.py"), "# fixture");
                if (Locate(Path.Combine(folder, "DevlinLauncher.exe"))[0] != folder) throw new Exception("Source discovery failed");
                File.Delete(Path.Combine(folder, "run.py")); Directory.CreateDirectory(Path.Combine(folder, "maintenance"));
                File.WriteAllText(Path.Combine(folder, @"maintenance\install-info.json"), new JavaScriptSerializer().Serialize(new { source_directory = folder }));
                if (Locate(Path.Combine(folder, "DevlinLauncher.exe"))[1] != folder) throw new Exception("Installed target discovery failed");
                checks.Add("checkout and installed-folder discovery");
                string fixture = Path.Combine(folder, "Проверка с пробелами.ps1");
                File.WriteAllText(fixture, "param([string]$Value)\n[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\nWrite-Host $Value\n[Console]::Error.WriteLine('stderr fixture')\nexit 7\n", new UTF8Encoding(true));
                string value = "D:\\Путь с пробелами\\ & $not_code \"quoted\"\\";
                var lines = new List<string>();
                int exit = Execute(InstallerProcess(new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", fixture, "-Value", value }, folder), delegate(string line) { lock (lines) lines.Add(line); });
                if (exit != 7 || !lines.Contains(value) || !lines.Contains("stderr fixture")) throw new Exception("Redirected process output, quoting or exit status failed");
                checks.Add("real PowerShell Unicode stdout/stderr, argument boundaries and exit 7");
            } catch (Exception e) { error = e.ToString(); }
            finally { if (Directory.Exists(folder)) Directory.Delete(folder, true); }
            File.WriteAllText(output, new JavaScriptSerializer().Serialize(new { ok = error == null, checks = checks, error = error }), Encoding.UTF8);
            return error == null ? 0 : 1;
        }
    }
    internal sealed class LauncherForm : Form {
        private readonly string workspace;
        private readonly TextBox source = new TextBox(), target = new TextBox();
        private readonly RichTextBox console = new RichTextBox();
        private readonly Label status = new Label();
        private readonly CheckBox local = new CheckBox(), shortcut = new CheckBox();
        private readonly Button install = new Button(), update = new Button(), open = new Button(), browseSource = new Button(), browseTarget = new Button();
        private readonly StreamWriter logfile;
        private readonly string logpath;
        private bool busy;
        internal LauncherForm(string workspace, string sourcePath, string targetPath) {
            this.workspace = workspace; Text = "Devlin Desk — установка и обновление"; MinimumSize = new Size(900, 650); Size = new Size(1100, 780); StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi; Font = new Font("Segoe UI", 11); BackColor = Color.FromArgb(246, 249, 243);
            Icon = new Icon(Path.Combine(workspace, "devlin-desk.ico"));
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(22), ColumnCount = 3, RowCount = 10 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120)); layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 110));
            for (int row = 0; row < 10; row++) layout.RowStyles.Add(new RowStyle(row == 7 ? SizeType.Percent : SizeType.Absolute, row == 7 ? 100 : row == 0 ? 52 : row == 1 ? 56 : row == 5 || row == 8 ? 50 : 40));
            Controls.Add(layout);
            var title = new Label { Text = "devlin  /  Установка приложения", Font = new Font("Segoe UI", 20, FontStyle.Bold), Dock = DockStyle.Fill }; layout.Controls.Add(title, 0, 0); layout.SetColumnSpan(title, 3);
            var hint = new Label { Text = "Перед установкой или обновлением выберите «Выход» в меню трея Devlin Desk.\nХод установки и ошибки отображаются ниже. Настройки и проекты сохраняются.", Dock = DockStyle.Fill }; layout.Controls.Add(hint, 0, 1); layout.SetColumnSpan(hint, 3);
            source.Text = sourcePath; target.Text = targetPath;
            AddPath(layout, "Исходники", source, browseSource, 2); AddPath(layout, "Приложение", target, browseTarget, 3);
            var options = new FlowLayoutPanel { Dock = DockStyle.Fill }; local.Text = "Обновлять из локальных исходников"; local.AutoSize = true; shortcut.Text = "Ярлык на рабочем столе"; shortcut.Checked = true; shortcut.AutoSize = true; options.Controls.Add(local); options.Controls.Add(shortcut); layout.Controls.Add(options, 0, 4); layout.SetColumnSpan(options, 3);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill }; SetupButton(install, "Установить", () => RunInstaller("Install")); SetupButton(update, "Обновить", () => RunInstaller("Update")); SetupButton(open, "Открыть приложение", OpenApplication); actions.Controls.AddRange(new Control[] { install, update, open }); layout.Controls.Add(actions, 0, 5); layout.SetColumnSpan(actions, 3);
            status.Text = "Готово к установке"; status.Dock = DockStyle.Fill; status.Padding = new Padding(0, 8, 0, 0); layout.Controls.Add(status, 0, 6); layout.SetColumnSpan(status, 3);
            console.ReadOnly = true; console.WordWrap = false; console.DetectUrls = false; console.Font = new Font("Consolas", 10); console.BackColor = Color.FromArgb(19, 28, 23); console.ForeColor = Color.FromArgb(218, 239, 209); console.Dock = DockStyle.Fill; layout.Controls.Add(console, 0, 7); layout.SetColumnSpan(console, 3);
            var save = new Button(); SetupButton(save, "Сохранить журнал", SaveLog); layout.Controls.Add(save, 1, 8);
            string logs = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"DevlinDeskLauncher\logs"); Directory.CreateDirectory(logs);
            logpath = Path.Combine(logs, DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".log"); logfile = new StreamWriter(logpath, false, new UTF8Encoding(false)) { AutoFlush = true };
            var logLabel = new Label { Text = "Журнал: " + logpath, AutoEllipsis = true, Dock = DockStyle.Fill }; layout.Controls.Add(logLabel, 0, 9); layout.SetColumnSpan(logLabel, 3);
            Append("Devlin Desk Launcher 1.10.0 — Windows x64"); SetBusy(false);
            target.TextChanged += delegate { if (!busy) SetBusy(false); };
            FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) { e.Cancel = true; status.Text = "Дождитесь завершения установки. Ход работы виден в журнале."; } };
            FormClosed += delegate { logfile.Dispose(); };
        }
        private static void SetupButton(Button button, string text, Action click) { button.Text = text; button.AutoSize = true; button.Padding = new Padding(10, 5, 10, 5); button.FlatStyle = FlatStyle.Flat; button.BackColor = Color.FromArgb(223, 243, 208); button.Click += delegate { click(); }; }
        private static void AddPath(TableLayoutPanel layout, string label, TextBox box, Button browse, int row) {
            layout.Controls.Add(new Label { Text = label, Dock = DockStyle.Fill, Padding = new Padding(0, 5, 0, 0) }, 0, row); box.Dock = DockStyle.Fill; layout.Controls.Add(box, 1, row); SetupButton(browse, "Выбрать", delegate { using (var dialog = new FolderBrowserDialog { SelectedPath = box.Text, Description = label }) if (dialog.ShowDialog() == DialogResult.OK) box.Text = dialog.SelectedPath; }); layout.Controls.Add(browse, 2, row);
        }
        private void SetBusy(bool value) { busy = value; install.Enabled = update.Enabled = source.Enabled = target.Enabled = browseSource.Enabled = browseTarget.Enabled = local.Enabled = shortcut.Enabled = !value; open.Enabled = !value && File.Exists(Path.Combine(target.Text, "DevlinDesk.exe")); }
        private void Append(string line) { logfile.WriteLine(line); console.AppendText(line + Environment.NewLine); console.SelectionStart = console.TextLength; console.ScrollToCaret(); }
        private async void RunInstaller(string action) {
            try {
                string root = Path.GetFullPath(source.Text), destination = Path.GetFullPath(target.Text);
                if (String.IsNullOrWhiteSpace(source.Text) || String.IsNullOrWhiteSpace(target.Text)) throw new ArgumentException("Укажите папки исходников и приложения.");
                if (local.Checked && !File.Exists(Path.Combine(root, "run.py"))) throw new IOException("Локальные исходники не найдены. Выберите папку репозитория или выключите локальный режим.");
                var args = new List<string> { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(workspace, "install-windows.ps1"), "-Action", action, "-SourceDirectory", root, "-TargetDirectory", destination };
                if (local.Checked) args.Add("-Local"); if (!shortcut.Checked) args.Add("-NoShortcut");
                SetBusy(true); status.Text = action == "Install" ? "Установка…" : "Обновление…"; Append("\n=== " + status.Text + " ===");
                var start = Program.InstallerProcess(args, workspace);
                int code = await Task.Run(delegate { return Program.Execute(start, delegate(string line) { BeginInvoke(new Action<string>(Append), line); }); });
                status.Text = code == 0 ? "Готово. Можно открыть приложение." : "Ошибка установки (код " + code + "). Подробности в журнале; исправьте причину и повторите.";
                Append(status.Text);
            } catch (Exception e) { status.Text = "Ошибка: " + e.Message; Append(e.ToString()); }
            finally { SetBusy(false); }
        }
        private void OpenApplication() { try { string exe = Path.Combine(Path.GetFullPath(target.Text), "DevlinDesk.exe"); if (!File.Exists(exe)) throw new FileNotFoundException("Приложение ещё не установлено."); Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = true }); } catch (Exception e) { status.Text = e.Message; Append(e.Message); } }
        private void SaveLog() { using (var dialog = new SaveFileDialog { FileName = "Devlin-install.log", Filter = "Журнал (*.log)|*.log" }) if (dialog.ShowDialog() == DialogResult.OK) { try { File.WriteAllText(dialog.FileName, console.Text, new UTF8Encoding(false)); } catch (Exception e) { status.Text = e.Message; } } }
    }
}
