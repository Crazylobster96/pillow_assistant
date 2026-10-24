// No-console starter for the offline bundle: runs launcher.pyw with the
// bundled pythonw.exe from the folder this exe lives in.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("Pillow Assistant")]
[assembly: System.Reflection.AssemblyProduct("Pillow Assistant")]

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "pythonw.exe");
        if (!File.Exists(python))
            python = Path.Combine(root, "python", "python.exe");
        string launcher = Path.Combine(root, "launcher.pyw");
        if (!File.Exists(python) || !File.Exists(launcher))
        {
            MessageBox.Show("Pillow Assistant 文件不完整，请重新解压发布包。\n\nMissing: " +
                (File.Exists(python) ? launcher : python), "Pillow Assistant",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string extra = string.Join(" ", args.Select(a => "\"" + a.Replace("\"", "\\\"") + "\""));
        var psi = new ProcessStartInfo(python, "-E -s \"" + launcher + "\" " + extra)
        {
            WorkingDirectory = root,
            UseShellExecute = false,
        };
        Process.Start(psi);
        return 0;
    }
}
