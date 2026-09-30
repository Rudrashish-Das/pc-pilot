// Starts the bot in the background: <repo>\.venv\Scripts\pythonw.exe -m llmbot, from the repo root.
// It exists so Task Manager > Startup apps shows "Discord LLM Bot" with the logo instead of "Python".
// Built by: .\scripts\bot_control.ps1 install   (output: bin\DiscordLLMBot.exe, which must stay in <repo>\bin)
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Discord LLM Bot")]
[assembly: AssemblyProduct("Discord LLM Bot")]
[assembly: AssemblyDescription("Starts the Discord/Telegram LLM bot in the background")]

static class Launcher {
    static int Main() {
        string root = Path.GetDirectoryName(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
        string pythonw = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        if (!File.Exists(pythonw)) {
            MessageBox.Show("Python venv not found:\n" + pythonw + "\n\nSee docs\\SETUP.md.", "Discord LLM Bot",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        // A second copy is harmless (the bot holds a single-instance lock), so no check here.
        var psi = new ProcessStartInfo(pythonw, "-m llmbot");
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        Process.Start(psi);
        return 0;
    }
}
