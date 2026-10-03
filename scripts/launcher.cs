// Starts the bot in the background: <repo>\.venv\Scripts\pythonw.exe -m llmbot, from the repo root.
// It exists so Task Manager > Startup apps shows "pc-pilot" with the logo instead of "Python".
// Built by: .\scripts\bot_control.ps1 install   (output: bin\pc-pilot.exe, which must stay in <repo>\bin)
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("pc-pilot")]
[assembly: AssemblyProduct("pc-pilot")]
[assembly: AssemblyDescription("Starts the Discord/Telegram LLM bot in the background")]

static class Launcher {
    static int Main() {
        string root = Path.GetDirectoryName(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
        string pythonw = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        if (!File.Exists(pythonw)) {
            MessageBox.Show("Python venv not found:\n" + pythonw + "\n\nSee docs\\SETUP.md.", "pc-pilot",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        // Already running: the bot holds this port (INSTANCE_PORT). A copy started at boot (bot_control.ps1 boot)
        // runs in session 0 without the user's DPAPI keys, so Claude Code jobs from it sign Chrome out of everything.
        // Ask it to hand over (it exits once no job is running: llmbot/core.py logon_handoff_watch) and wait.
        if (!PortFree()) {
            if (Process.GetCurrentProcess().SessionId == 0 || !BootCopyRunning()) return 0;
            string handoff = Path.Combine(root, @"bin\logon-handoff");
            try {
                File.WriteAllText(handoff, Process.GetCurrentProcess().Id.ToString());
                var deadline = System.DateTime.Now.AddHours(6);
                while (!PortFree()) {
                    if (System.DateTime.Now > deadline) return 0;
                    System.Threading.Thread.Sleep(2000);
                }
                System.Threading.Thread.Sleep(1000);  // the old copy finishing its exit
            } finally {
                try { File.Delete(handoff); } catch (IOException) {}
            }
        }
        var psi = new ProcessStartInfo(pythonw, "-m llmbot");
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        Process.Start(psi);
        return 0;
    }

    static bool PortFree() {
        try {
            var probe = new TcpListener(IPAddress.Loopback, 47823);
            probe.ExclusiveAddressUse = true;
            probe.Start();
            probe.Stop();
            return true;
        } catch (SocketException) {
            return false;
        }
    }

    static bool BootCopyRunning() {
        foreach (var name in new[] { "pythonw", "python" })
            foreach (var p in Process.GetProcessesByName(name))
                try { if (p.SessionId == 0) return true; } catch (System.Exception) {}
        return false;
    }
}
