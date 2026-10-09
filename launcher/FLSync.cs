// FLSync.exe: starts FLSync (app\flsync.py next to this file) with FL Studio's
// own Python, without a console window. Built with the C# compiler that comes
// with Windows (scripts/build_exe.py); C# 5, .NET Framework 4.
//
// Started from anywhere but the installed copy (%LOCALAPPDATA%\FLSync\app),
// that is from the unpacked download, FLSync opens its installer first
// (--install). Other arguments are passed on (the Start menu's --repair and
// --uninstall, and flsync.py's own options).
//
// --launcher-check prints the command it would run, and runs nothing.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("FLSync")]
[assembly: AssemblyProduct("FLSync")]
[assembly: AssemblyDescription("Jobba i samma FL Studio-projekt som andra, live")]
// AssemblyVersion and AssemblyFileVersion: app/version.py's, written by scripts/build_exe.py.

static class Launcher
{
    const string Title = "FLSync";

    [STAThread]
    static int Main(string[] args)
    {
        string here = Path.GetDirectoryName(Path.GetFullPath(Assembly.GetExecutingAssembly().Location));
        string script = Path.Combine(Path.Combine(here, "app"), "flsync.py");
        bool check = Array.IndexOf(args, "--launcher-check") >= 0;
        if (!File.Exists(script))
        {
            return Fail(check, "Hittar inte resten av FLSync bredvid FLSync.exe.\n\nPacka upp hela zip-filen först " +
                        "(högerklicka på den och välj Extrahera alla) och starta FLSync.exe i den uppackade mappen.");
        }
        string python = FindPython();
        if (python == null)
        {
            return Fail(check, "Hittar ingen Python att köra FLSync med.\n\nFLSync använder FL Studios egen Python: " +
                        "installera FL Studio 2024 eller senare och försök igen.");
        }
        List<string> parts = new List<string>();
        parts.Add(Quote(script));
        if (!IsInstalledCopy(here) && Array.IndexOf(args, "--uninstall") < 0 && Array.IndexOf(args, "--repair") < 0)
        {
            parts.Add("--install");  // the download: install (or update) first
        }
        foreach (string arg in args)
        {
            if (arg != "--launcher-check")
            {
                parts.Add(Quote(arg));
            }
        }
        string arguments = string.Join(" ", parts.ToArray());
        if (check)
        {
            Console.Out.WriteLine(Quote(python) + " " + arguments);
            return 0;
        }
        ProcessStartInfo info = new ProcessStartInfo(python, arguments);
        info.WorkingDirectory = here;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;  // python.exe (no pythonw.exe) gets no console either
        info.EnvironmentVariables["FLSYNC_EXE"] = Assembly.GetExecutingAssembly().Location;
        try
        {
            Process.Start(info);
        }
        catch (Exception e)
        {
            return Fail(false, "FLSync kunde inte starta:\n\n" + e.Message);
        }
        return 0;
    }

    static int Fail(bool check, string text)
    {
        if (check)
        {
            Console.Out.WriteLine(text);
        }
        else
        {
            MessageBox.Show(text, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        return 1;
    }

    // The installed copy: %LOCALAPPDATA%\FLSync\app (setup/setup.py puts it there).
    static bool IsInstalledCopy(string here)
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string installed = Path.Combine(Path.Combine(local, "FLSync"), "app");
        return string.Equals(Path.GetFullPath(here).TrimEnd('\\'), Path.GetFullPath(installed).TrimEnd('\\'),
                             StringComparison.OrdinalIgnoreCase);
    }

    // The newest FL Studio's own pythonw.exe (python.exe if it has no pythonw),
    // like the installer: FL Studio <n> under the Program Files folders, and
    // where FL says it is installed. Else a pythonw on the PATH.
    static string FindPython()
    {
        string best = null;
        int bestNumber = -1;
        List<string> roots = new List<string>();
        roots.Add(Environment.GetEnvironmentVariable("ProgramW6432"));
        roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles));
        roots.Add(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86));
        foreach (string root in roots)
        {
            if (string.IsNullOrEmpty(root))
            {
                continue;
            }
            string imageLine = Path.Combine(root, "Image-Line");
            if (!Directory.Exists(imageLine))
            {
                continue;
            }
            try
            {
                foreach (string folder in Directory.GetDirectories(imageLine, "FL Studio *"))
                {
                    Consider(folder, ref best, ref bestNumber);
                }
            }
            catch (Exception)
            {
                // a folder we may not read: the others still count
            }
        }
        string installed = Registry.GetValue(@"HKEY_CURRENT_USER\Software\Image-Line\Shared\Paths", "Install path",
                                             null) as string;
        if (!string.IsNullOrEmpty(installed))
        {
            Consider(installed.TrimEnd('\\'), ref best, ref bestNumber);
        }
        if (best != null)
        {
            return best;
        }
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string folder in path.Split(';'))
        {
            try
            {
                string candidate = Path.Combine(folder.Trim().Trim('"'), "pythonw.exe");
                if (folder.Trim().Length > 0 && File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (ArgumentException)
            {
                // a malformed PATH entry
            }
        }
        return null;
    }

    static void Consider(string folder, ref string best, ref int bestNumber)
    {
        string python = Path.Combine(Path.Combine(Path.Combine(folder, "Shared"), "Python"), "pythonw.exe");
        if (!File.Exists(python))
        {
            python = Path.Combine(Path.GetDirectoryName(python), "python.exe");
            if (!File.Exists(python))
            {
                return;
            }
        }
        string name = Path.GetFileName(folder);  // "FL Studio 2025", "FL Studio 21"
        int number;
        if (!int.TryParse(name.Replace("FL Studio", "").Trim(), out number))
        {
            number = 0;
        }
        if (number > bestNumber)
        {
            best = python;
            bestNumber = number;
        }
    }

    // One argument as CommandLineToArgvW reads it back.
    static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0)
        {
            return arg;
        }
        StringBuilder quoted = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                quoted.Append('\\', backslashes * 2 + 1);
            }
            else
            {
                quoted.Append('\\', backslashes);
            }
            backslashes = 0;
            quoted.Append(c);
        }
        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}
