using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class ProcessingPreset
{
    public string Name { get; set; }
    public bool BuiltIn { get; set; }
    public double MasterStrength { get; set; }
    public bool TempoEnabled { get; set; }
    public double TempoPercent { get; set; }
    public bool EqEnabled { get; set; }
    public double Eq120 { get; set; }
    public double Eq200 { get; set; }
    public double Eq500 { get; set; }
    public double Eq3500 { get; set; }
    public double Eq5000 { get; set; }
    public double Eq10000 { get; set; }
    public bool DeEsserEnabled { get; set; }
    public double DeEsserReduction { get; set; }
    public bool CompressorEnabled { get; set; }
    public double CompressorThreshold { get; set; }
    public double CompressorRatio { get; set; }
    public double CompressorKnee { get; set; }
    public double CompressorAttack { get; set; }
    public double CompressorRelease { get; set; }
    public bool LoudnessEnabled { get; set; }
    public string LoudnessMode { get; set; }
    public double MonoTarget { get; set; }
    public double StereoTarget { get; set; }
    public bool LimiterEnabled { get; set; }
    public double LimiterCeiling { get; set; }

    public ProcessingPreset Clone(string newName, bool builtIn)
    {
        return new ProcessingPreset {
            Name = newName, BuiltIn = builtIn, MasterStrength = MasterStrength,
            TempoEnabled = TempoEnabled, TempoPercent = TempoPercent,
            EqEnabled = EqEnabled, Eq120 = Eq120, Eq200 = Eq200, Eq500 = Eq500,
            Eq3500 = Eq3500, Eq5000 = Eq5000, Eq10000 = Eq10000,
            DeEsserEnabled = DeEsserEnabled, DeEsserReduction = DeEsserReduction,
            CompressorEnabled = CompressorEnabled,
            CompressorThreshold = CompressorThreshold, CompressorRatio = CompressorRatio,
            CompressorKnee = CompressorKnee, CompressorAttack = CompressorAttack,
            CompressorRelease = CompressorRelease, LoudnessEnabled = LoudnessEnabled,
            LoudnessMode = LoudnessMode, MonoTarget = MonoTarget, StereoTarget = StereoTarget,
            LimiterEnabled = LimiterEnabled, LimiterCeiling = LimiterCeiling
        };
    }

    public static ProcessingPreset Create(string name, string mode, double mono, double stereo, double ceiling)
    {
        return new ProcessingPreset {
            Name = name, BuiltIn = true, MasterStrength = 100,
            TempoEnabled = false, TempoPercent = -3,
            EqEnabled = true, Eq120 = 1, Eq200 = 1.5, Eq500 = -1,
            Eq3500 = -1.5, Eq5000 = -2, Eq10000 = -1,
            DeEsserEnabled = false, DeEsserReduction = 2,
            CompressorEnabled = true, CompressorThreshold = -18,
            CompressorRatio = 2, CompressorKnee = 6,
            CompressorAttack = 15, CompressorRelease = 125,
            LoudnessEnabled = true, LoudnessMode = mode,
            MonoTarget = mono, StereoTarget = stereo,
            LimiterEnabled = true, LimiterCeiling = ceiling
        };
    }
}

internal sealed class PresetFile
{
    public int Version { get; set; }
    public List<ProcessingPreset> Presets { get; set; }
    public PresetFile() { Version = 1; Presets = new List<ProcessingPreset>(); }
}

internal sealed class AppSettings
{
    public int Version { get; set; }
    public string LastPreset { get; set; }
    public string ExportProfile { get; set; }
    public string CustomFormat { get; set; }
    public string SampleRate { get; set; }
    public string Channels { get; set; }
    public string BitrateMode { get; set; }
    public int Bitrate { get; set; }
    public int Workers { get; set; }
    public string Suffix { get; set; }
    public bool UseOutputFolder { get; set; }
    public string OutputFolder { get; set; }

    public AppSettings()
    {
        Version = 1; LastPreset = "Audiobook"; ExportProfile = "Same as Source";
        CustomFormat = "MP3"; SampleRate = "Preserve"; Channels = "Preserve";
        BitrateMode = "VBR"; Bitrate = 96; Workers = 2; Suffix = "_processed";
        UseOutputFolder = false; OutputFolder = "";
    }
}

internal sealed class ExportOptions
{
    public string Profile;
    public string Format;
    public string SampleRate;
    public string Channels;
    public string BitrateMode;
    public int Bitrate;
    public string Suffix;
    public bool UseOutputFolder;
    public string OutputFolder;

    public ExportOptions Clone()
    {
        return (ExportOptions)MemberwiseClone();
    }
}

internal sealed class AudioJob
{
    public string Source;
    public string Output;
    public string Status;
    public int Progress;
    public string Error;
    public ListViewItem Item;
}

internal sealed class ProbeInfo
{
    public double Duration;
    public int Channels;
}

internal sealed class ProcessResult
{
    public int ExitCode;
    public string ErrorText;
}

internal static class OutputPlanner
{
    public static string Choose(string directory, string baseName, string extension, ISet<string> reserved)
    {
        string candidate = Path.Combine(directory, baseName + extension);
        int number = 2;
        while (File.Exists(candidate) || reserved.Contains(candidate))
        {
            candidate = Path.Combine(directory, baseName + " (" + number + ")" + extension);
            number++;
        }
        reserved.Add(candidate);
        return candidate;
    }
}

internal sealed class ProcessingEngine
{
    private readonly string ffmpegPath;
    private readonly object processLock = new object();
    private readonly List<Process> activeProcesses = new List<Process>();
    private volatile bool cancelRequested;

    public ProcessingEngine(string ffmpeg)
    {
        ffmpegPath = ffmpeg;
    }

    public void ResetCancellation() { cancelRequested = false; }

    public void Cancel()
    {
        cancelRequested = true;
        lock (processLock)
        {
            foreach (Process process in activeProcesses.ToArray())
            {
                try { if (!process.HasExited) process.Kill(); } catch { }
            }
        }
    }

    public ProbeInfo Probe(string input)
    {
        var startInfo = new ProcessStartInfo {
            FileName = ffmpegPath,
            Arguments = "-hide_banner -i " + Quote(input),
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardError = true, RedirectStandardOutput = true
        };
        using (Process process = Process.Start(startInfo))
        {
            string stderr = process.StandardError.ReadToEnd();
            process.WaitForExit();
            Match duration = Regex.Match(stderr, @"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)");
            if (!duration.Success || !Regex.IsMatch(stderr, @"Audio:\s", RegexOptions.IgnoreCase))
                throw new InvalidOperationException("No readable audio stream was found.");
            double seconds = int.Parse(duration.Groups[1].Value) * 3600.0
                + int.Parse(duration.Groups[2].Value) * 60.0
                + double.Parse(duration.Groups[3].Value, CultureInfo.InvariantCulture);
            Match audioLine = Regex.Match(stderr, @"Audio:.*", RegexOptions.IgnoreCase);
            string line = audioLine.Success ? audioLine.Value.ToLowerInvariant() : "";
            int channels = line.Contains("mono") ? 1 : (line.Contains("stereo") ? 2 : 2);
            return new ProbeInfo { Duration = seconds, Channels = channels };
        }
    }

    public void ProcessFile(AudioJob job, ProcessingPreset preset, ExportOptions export, Action<int, string> report)
    {
        if (cancelRequested) throw new OperationCanceledException();
        ProbeInfo probe = Probe(job.Source);
        string pre = BuildPreMasterFilters(preset);
        double target = probe.Channels == 1 ? preset.MonoTarget : preset.StereoTarget;
        string normalize = "";

        if (preset.LoudnessEnabled)
        {
            report(2, preset.LoudnessMode == "RMS" ? "Measuring RMS" : "Measuring loudness");
            if (preset.LoudnessMode == "RMS")
            {
                string rmsFilter = JoinFilters(pre, "volumedetect");
                string rmsArgs = "-y -hide_banner -loglevel info -i " + Quote(job.Source)
                    + " -map 0:a:0 -af " + Quote(rmsFilter)
                    + " -vn -f null NUL";
                ProcessResult measured = RunProcess(rmsArgs, probe.Duration, 2, 25, null);
                if (measured.ExitCode != 0) throw new InvalidOperationException(CleanError(measured.ErrorText));
                Match mean = Regex.Match(measured.ErrorText, @"mean_volume:\s*(-?[0-9.]+)\s*dB", RegexOptions.IgnoreCase);
                if (!mean.Success) throw new InvalidOperationException("RMS measurement did not return a mean volume.");
                double meanDb = double.Parse(mean.Groups[1].Value, CultureInfo.InvariantCulture);
                normalize = "volume=" + F(target - meanDb) + "dB";
            }
            else
            {
                string measureFilter = JoinFilters(pre,
                    "loudnorm=I=" + F(target) + ":LRA=7:TP=" + F(preset.LimiterCeiling) + ":print_format=json");
                string measureArgs = "-y -hide_banner -loglevel info -i " + Quote(job.Source)
                    + " -map 0:a:0 -af " + Quote(measureFilter)
                    + " -vn -f null NUL";
                ProcessResult measured = RunProcess(measureArgs, probe.Duration, 2, 25, null);
                if (measured.ExitCode != 0) throw new InvalidOperationException(CleanError(measured.ErrorText));
                normalize = BuildSecondPassLoudnorm(measured.ErrorText, target, preset.LimiterCeiling);
            }
        }

        string filters = JoinFilters(pre, normalize);
        if (preset.LimiterEnabled)
        {
            double limit = Math.Pow(10.0, preset.LimiterCeiling / 20.0);
            filters = JoinFilters(filters,
                "alimiter=limit=" + F(limit) + ":attack=10:release=50:level=false:latency=true");
        }

        string temporary = Path.Combine(Path.GetDirectoryName(job.Output),
            "." + Path.GetFileNameWithoutExtension(job.Output) + "." + Guid.NewGuid().ToString("N")
            + ".tmp" + Path.GetExtension(job.Output));
        try
        {
            report(25, "Processing");
            var args = new StringBuilder();
            args.Append("-y -hide_banner -loglevel error -i ").Append(Quote(job.Source));
            args.Append(" -map 0:a:0 -map_metadata 0 -vn");
            if (!string.IsNullOrWhiteSpace(filters)) args.Append(" -af ").Append(Quote(filters));
            AddOutputArguments(args, job.Source, job.Output, export);
            args.Append(" ").Append(Quote(temporary));
            ProcessResult rendered = RunProcess(args.ToString(), probe.Duration, 25, 75,
                delegate(int progress) { report(progress, "Processing"); });
            if (cancelRequested) throw new OperationCanceledException();
            if (rendered.ExitCode != 0) throw new InvalidOperationException(CleanError(rendered.ErrorText));
            if (!File.Exists(temporary) || new FileInfo(temporary).Length == 0)
                throw new InvalidOperationException("FFmpeg produced no output file.");
            File.Move(temporary, job.Output);
            report(100, "Completed");
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); } catch { }
        }
    }

    private ProcessResult RunProcess(string arguments, double duration, int baseProgress, int span, Action<int> progress)
    {
        if (cancelRequested) throw new OperationCanceledException();
        var startInfo = new ProcessStartInfo {
            FileName = ffmpegPath,
            Arguments = arguments + " -progress pipe:1 -nostats",
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        var errors = new StringBuilder();
        using (var process = new Process())
        {
            process.StartInfo = startInfo;
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) {
                if (e.Data != null) lock (errors) errors.AppendLine(e.Data);
            };
            process.Start();
            lock (processLock) activeProcesses.Add(process);
            process.BeginErrorReadLine();
            try
            {
                string line;
                while ((line = process.StandardOutput.ReadLine()) != null)
                {
                    if (cancelRequested) { try { process.Kill(); } catch { } break; }
                    if (line.StartsWith("out_time_us=", StringComparison.OrdinalIgnoreCase))
                    {
                        long value;
                        if (long.TryParse(line.Substring(12), out value) && duration > 0)
                        {
                            int percent = baseProgress + (int)Math.Min(span,
                                Math.Max(0, value / 1000000.0 / duration * span));
                            if (progress != null) progress(percent);
                        }
                    }
                }
                process.WaitForExit();
                if (cancelRequested) throw new OperationCanceledException();
                return new ProcessResult { ExitCode = process.ExitCode, ErrorText = errors.ToString() };
            }
            finally
            {
                lock (processLock) activeProcesses.Remove(process);
            }
        }
    }

    private static string BuildPreMasterFilters(ProcessingPreset preset)
    {
        double amount = Math.Max(0, Math.Min(1, preset.MasterStrength / 100.0));
        var filters = new List<string>();
        if (preset.TempoEnabled && amount > 0)
        {
            double tempo = 1.0 + preset.TempoPercent / 100.0 * amount;
            filters.Add("rubberband=tempo=" + F(tempo)
                + ":pitch=1:transients=smooth:detector=soft:phase=laminar:window=long:smoothing=on:formant=preserved");
        }
        if (preset.EqEnabled && amount > 0)
        {
            filters.Add("highpass=f=60:p=2");
            filters.Add(Eq(120, preset.Eq120 * amount));
            filters.Add(Eq(200, preset.Eq200 * amount));
            filters.Add(Eq(500, preset.Eq500 * amount));
            filters.Add(Eq(3500, preset.Eq3500 * amount));
            filters.Add(Eq(5000, preset.Eq5000 * amount));
            filters.Add(Eq(10000, preset.Eq10000 * amount));
        }
        if (preset.DeEsserEnabled && amount > 0)
        {
            double reduction = Math.Min(4, Math.Max(0, preset.DeEsserReduction)) * amount;
            double maximum = 1.0 - Math.Pow(10.0, -reduction / 20.0);
            filters.Add("deesser=i=" + F(0.5 * amount) + ":m=" + F(maximum) + ":f=0.5:s=o");
        }
        if (preset.CompressorEnabled && amount > 0)
        {
            double threshold = Math.Pow(10.0, preset.CompressorThreshold / 20.0);
            double knee = Math.Pow(10.0, preset.CompressorKnee / 20.0);
            filters.Add("acompressor=threshold=" + F(threshold)
                + ":ratio=" + F(preset.CompressorRatio)
                + ":knee=" + F(knee)
                + ":attack=" + F(preset.CompressorAttack)
                + ":release=" + F(preset.CompressorRelease)
                + ":makeup=1:detection=rms:mix=" + F(amount));
        }
        return string.Join(",", filters.ToArray());
    }

    private static string BuildSecondPassLoudnorm(string text, double target, double ceiling)
    {
        string inputI = JsonValue(text, "input_i");
        string inputTp = JsonValue(text, "input_tp");
        string inputLra = JsonValue(text, "input_lra");
        string inputThresh = JsonValue(text, "input_thresh");
        string offset = JsonValue(text, "target_offset");
        if (inputI == null || inputTp == null || inputLra == null || inputThresh == null || offset == null)
            throw new InvalidOperationException("Loudness measurement did not return complete statistics.");
        return "loudnorm=I=" + F(target) + ":LRA=7:TP=" + F(ceiling)
            + ":measured_I=" + inputI + ":measured_TP=" + inputTp
            + ":measured_LRA=" + inputLra + ":measured_thresh=" + inputThresh
            + ":offset=" + offset + ":linear=true:print_format=summary";
    }

    private static string JsonValue(string text, string name)
    {
        Match match = Regex.Match(text, "\"" + Regex.Escape(name) + "\"\\s*:\\s*\"?(-?[0-9.]+|[-a-z]+)\"?", RegexOptions.IgnoreCase);
        return match.Success ? match.Groups[1].Value : null;
    }

    private static void AddOutputArguments(StringBuilder args, string source, string output, ExportOptions options)
    {
        string extension = Path.GetExtension(output).ToLowerInvariant();
        if (options.Channels == "Mono") args.Append(" -ac 1");
        else if (options.Channels == "Stereo") args.Append(" -ac 2");
        if (options.SampleRate == "24000") args.Append(" -ar 24000");
        else if (options.SampleRate == "48000") args.Append(" -ar 48000");

        if (extension == ".wav") args.Append(" -c:a pcm_s16le");
        else if (extension == ".aif" || extension == ".aiff") args.Append(" -c:a pcm_s16be");
        else if (extension == ".flac") args.Append(" -c:a flac -compression_level 8");
        else if (extension == ".mp3")
        {
            args.Append(" -c:a libmp3lame");
            if (options.BitrateMode == "VBR") args.Append(" -q:a 2");
            else args.Append(" -b:a ").Append(options.Bitrate).Append("k -abr ").Append(options.BitrateMode == "ABR" ? "1" : "0");
            args.Append(" -id3v2_version 3");
        }
        else if (extension == ".opus")
            args.Append(" -c:a libopus -b:a ").Append(options.Bitrate).Append("k -vbr ").Append(options.BitrateMode == "CBR" ? "off" : "on");
        else if (extension == ".ogg")
            args.Append(options.BitrateMode == "VBR" ? " -c:a libvorbis -q:a 6" : " -c:a libvorbis -b:a " + options.Bitrate + "k");
        else if (extension == ".wma") args.Append(" -c:a wmav2 -b:a ").Append(options.Bitrate).Append("k");
        else if (extension == ".m4a" || extension == ".aac" || extension == ".mp4" || extension == ".m4b")
            args.Append(" -c:a aac -b:a ").Append(options.Bitrate).Append("k");
        else throw new InvalidOperationException("Unsupported output format: " + extension);
    }

    private static string Eq(int frequency, double gain)
    {
        return "equalizer=f=" + frequency + ":t=q:w=1:g=" + F(gain);
    }

    private static string JoinFilters(params string[] values)
    {
        return string.Join(",", values.Where(delegate(string value) { return !string.IsNullOrWhiteSpace(value); }).ToArray());
    }

    private static string CleanError(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return "FFmpeg returned an error.";
        string[] lines = text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
        return string.Join(" | ", lines.Take(5).ToArray());
    }

    private static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
    private static string F(double value) { return value.ToString("0.######", CultureInfo.InvariantCulture); }
}

internal sealed class PromptDialog : Form
{
    private readonly TextBox input;
    public string Value { get { return input.Text.Trim(); } }

    public PromptDialog(string title, string label, string initial)
    {
        Text = title; ClientSize = new Size(390, 120); FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterParent; MaximizeBox = false; MinimizeBox = false;
        var caption = new Label { Text = label, AutoSize = true, Location = new Point(12, 14) };
        input = new TextBox { Text = initial, Location = new Point(12, 38), Width = 365 };
        var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Location = new Point(221, 78), Width = 75 };
        var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Location = new Point(302, 78), Width = 75 };
        Controls.AddRange(new Control[] { caption, input, ok, cancel }); AcceptButton = ok; CancelButton = cancel;
    }
}

internal sealed class AudioPostProcessorForm : Form
{
    private static readonly HashSet<string> Supported = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        ".wav", ".mp3", ".m4a", ".aac", ".mp4", ".m4b", ".ogg", ".opus", ".wma", ".flac", ".aif", ".aiff"
    };
    private readonly string appDirectory = AppDomain.CurrentDomain.BaseDirectory;
    private readonly string settingsPath;
    private readonly string presetsPath;
    private readonly string ffmpegPath;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly List<AudioJob> jobs = new List<AudioJob>();
    private readonly List<ProcessingPreset> userPresets = new List<ProcessingPreset>();
    private readonly List<ProcessingPreset> builtIns = new List<ProcessingPreset>();
    private AppSettings settings;
    private ProcessingEngine engine;
    private bool running;

    private ComboBox presetCombo, exportProfileCombo, formatCombo, sampleRateCombo, channelsCombo, bitrateModeCombo;
    private NumericUpDown workersBox, bitrateBox, masterBox, tempoBox, deEsserBox;
    private NumericUpDown eq120, eq200, eq500, eq3500, eq5000, eq10000;
    private NumericUpDown thresholdBox, ratioBox, kneeBox, attackBox, releaseBox, monoTargetBox, stereoTargetBox, ceilingBox;
    private CheckBox tempoCheck, eqCheck, deEsserCheck, compressorCheck, loudnessCheck, limiterCheck, outputFolderCheck;
    private TextBox suffixBox, outputFolderBox;
    private ListView queue;
    private Button startButton, cancelButton, retryButton;
    private ProgressBar overallProgress;
    private Label overallLabel;

    public AudioPostProcessorForm()
    {
        settingsPath = Path.Combine(appDirectory, "settings.json");
        presetsPath = Path.Combine(appDirectory, "presets.json");
        ffmpegPath = LocateFfmpeg();
        settings = LoadSettings();
        BuildPresets();
        LoadUserPresets();
        InitializeUi();
        LoadSettingsIntoUi();
        FormClosing += OnFormClosing;
    }

    private void InitializeUi()
    {
        Text = "Audio Post Processor"; ClientSize = new Size(1160, 760); MinimumSize = new Size(980, 680);
        StartPosition = FormStartPosition.CenterScreen; Font = new Font("Segoe UI", 9F);
        AutoScaleDimensions = new SizeF(96F, 96F); AutoScaleMode = AutoScaleMode.Dpi; AllowDrop = true;
        DragEnter += OnDragEnter; DragDrop += OnDragDrop;

        var tabs = new TabControl { Dock = DockStyle.Fill };
        var queueTab = new TabPage("Batch Queue");
        var advancedTab = new TabPage("Advanced Processing");
        tabs.TabPages.Add(queueTab); tabs.TabPages.Add(advancedTab); Controls.Add(tabs);

        var queueLayout = new TableLayoutPanel {
            Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, Padding = Padding.Empty
        };
        queueLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        queueLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 124F));
        queueLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        queueLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        queueTab.Controls.Add(queueLayout);

        var header = new TableLayoutPanel {
            Dock = DockStyle.Fill, AutoSize = false,
            ColumnCount = 1, RowCount = 3, Margin = Padding.Empty, Padding = new Padding(10, 8, 10, 8)
        };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        header.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        header.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        header.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));

        var commandRow = FlowRow();
        commandRow.Controls.Add(FlowButton("Add Files", delegate { AddFiles(); }, 82));
        commandRow.Controls.Add(FlowButton("Add Folder", delegate { AddFolder(); }, 82));
        commandRow.Controls.Add(FlowButton("Remove", delegate { RemoveSelected(); }, 82));
        commandRow.Controls.Add(FlowButton("Clear", delegate { if (!running) { jobs.Clear(); queue.Items.Clear(); UpdateOverall(); } }, 82));
        commandRow.Controls.Add(FlowLabel("Processing preset", 100));
        presetCombo = FlowCombo(161); presetCombo.SelectedIndexChanged += delegate { ApplySelectedPreset(); };
        commandRow.Controls.Add(presetCombo);
        commandRow.Controls.Add(FlowButton("Save", delegate { SavePreset(); }, 62));
        commandRow.Controls.Add(FlowButton("Duplicate", delegate { DuplicatePreset(); }, 78));
        commandRow.Controls.Add(FlowButton("Rename", delegate { RenamePreset(); }, 70));
        commandRow.Controls.Add(FlowButton("Delete", delegate { DeletePreset(); }, 66));

        var settingsRow = FlowRow();
        settingsRow.Controls.Add(FlowLabel("Master strength", 96));
        masterBox = FlowNumber(70, 0, 100, 100, 0); settingsRow.Controls.Add(masterBox);
        settingsRow.Controls.Add(FlowLabel("%", 16));
        settingsRow.Controls.Add(FlowLabel("Workers", 52));
        workersBox = FlowNumber(62, 1, 8, 2, 0); settingsRow.Controls.Add(workersBox);
        settingsRow.Controls.Add(FlowLabel("Export", 42));
        exportProfileCombo = FlowCombo(150); exportProfileCombo.Items.AddRange(new object[] { "Same as Source", "Spoken Opus", "Compatible MP3", "Custom" });
        exportProfileCombo.SelectedIndexChanged += delegate { ApplyExportProfile(); }; settingsRow.Controls.Add(exportProfileCombo);
        settingsRow.Controls.Add(FlowLabel("Suffix", 40));
        suffixBox = new TextBox { Width = 105, Text = "_processed", Margin = new Padding(3, 4, 8, 3) }; settingsRow.Controls.Add(suffixBox);

        var outputRow = FlowRow();
        outputFolderCheck = new CheckBox { Text = "Use output folder", AutoSize = true, Margin = new Padding(3, 6, 5, 3) };
        outputFolderCheck.CheckedChanged += delegate { outputFolderBox.Enabled = outputFolderCheck.Checked; };
        outputFolderBox = new TextBox { Width = 360, Enabled = false, Margin = new Padding(3, 4, 3, 3) };
        outputRow.Controls.Add(outputFolderCheck); outputRow.Controls.Add(outputFolderBox);
        outputRow.Controls.Add(FlowButton("...", delegate { ChooseOutputFolder(); }, 36));

        header.Controls.Add(commandRow, 0, 0); header.Controls.Add(settingsRow, 0, 1); header.Controls.Add(outputRow, 0, 2);
        queueLayout.Controls.Add(header, 0, 0);

        queue = new ListView { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, GridLines = true, HideSelection = false };
        queue.Columns.Add("Source", 390); queue.Columns.Add("Format", 65); queue.Columns.Add("Output", 390);
        queue.Columns.Add("Status", 150); queue.Columns.Add("Progress", 80);
        queue.SizeChanged += delegate { ResizeQueueColumns(); };
        queueLayout.Controls.Add(queue, 0, 1);

        var bottom = new TableLayoutPanel {
            Dock = DockStyle.Fill, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            ColumnCount = 6, RowCount = 1, Margin = Padding.Empty, Padding = new Padding(10, 9, 10, 9)
        };
        bottom.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); bottom.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        bottom.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); bottom.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F)); bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 190F));
        startButton = FlowButton("Start Processing", delegate { StartProcessing(); }, 125);
        cancelButton = FlowButton("Cancel", delegate { CancelProcessing(); }, 80); cancelButton.Enabled = false;
        retryButton = FlowButton("Retry Failed", delegate { RetryFailed(); }, 95);
        var open = FlowButton("Open Output", delegate { OpenSelectedOutput(); }, 100);
        overallProgress = new ProgressBar { Dock = DockStyle.Fill, MinimumSize = new Size(100, 23), Margin = new Padding(12, 5, 12, 3) };
        overallLabel = new Label { Dock = DockStyle.Fill, AutoEllipsis = true, TextAlign = ContentAlignment.MiddleRight, Margin = new Padding(3) };
        bottom.Controls.Add(startButton, 0, 0); bottom.Controls.Add(cancelButton, 1, 0); bottom.Controls.Add(retryButton, 2, 0);
        bottom.Controls.Add(open, 3, 0); bottom.Controls.Add(overallProgress, 4, 0); bottom.Controls.Add(overallLabel, 5, 0);
        queueLayout.Controls.Add(bottom, 0, 2);

        BuildAdvancedTab(advancedTab);
        RefreshPresetCombo(settings.LastPreset);
    }

    private void BuildAdvancedTab(TabPage tab)
    {
        var scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(18) }; tab.Controls.Add(scroll);
        int y = 15;
        var tempoGroup = Group("Tempo", y, 108); scroll.Controls.Add(tempoGroup); y += 116;
        tempoCheck = CheckAt("Enable pitch-preserving tempo change", 16, 27); tempoGroup.Controls.Add(tempoCheck);
        tempoGroup.Controls.Add(LabelAt("Percent", 330, 30)); tempoBox = NumberAt(390, 25, -7, 0, -3, 1); tempoGroup.Controls.Add(tempoBox);
        tempoGroup.Controls.Add(LabelAt("Recommended: -2% to -5%; disabled by default.", 16, 63));

        var eqGroup = Group("Filter Curve EQ", y, 150); scroll.Controls.Add(eqGroup); y += 158;
        eqCheck = CheckAt("Enable EQ and 60 Hz roll-off", 16, 27); eqGroup.Controls.Add(eqCheck);
        int[] freqs = { 120, 200, 500, 3500, 5000, 10000 }; NumericUpDown[] boxes = new NumericUpDown[6];
        for (int i = 0; i < freqs.Length; i++)
        {
            int x = 16 + (i % 3) * 245, row = 58 + (i / 3) * 38;
            eqGroup.Controls.Add(LabelAt(freqs[i].ToString("N0") + " Hz", x, row + 4));
            boxes[i] = NumberAt(x + 80, row, -12, 12, 0, 1); eqGroup.Controls.Add(boxes[i]);
            eqGroup.Controls.Add(LabelAt("dB", x + 145, row + 4));
        }
        eq120 = boxes[0]; eq200 = boxes[1]; eq500 = boxes[2]; eq3500 = boxes[3]; eq5000 = boxes[4]; eq10000 = boxes[5];

        var deGroup = Group("De-esser", y, 105); scroll.Controls.Add(deGroup); y += 113;
        deEsserCheck = CheckAt("Enable dynamic 5–8 kHz de-essing", 16, 27); deGroup.Controls.Add(deEsserCheck);
        deGroup.Controls.Add(LabelAt("Maximum reduction", 330, 30)); deEsserBox = NumberAt(450, 25, 0, 4, 2, 1); deGroup.Controls.Add(deEsserBox); deGroup.Controls.Add(LabelAt("dB", 515, 30));
        deGroup.Controls.Add(LabelAt("Use only for sharp S, SH, and CH sounds.", 16, 63));

        var compGroup = Group("Compressor", y, 145); scroll.Controls.Add(compGroup); y += 153;
        compressorCheck = CheckAt("Enable subtle compression (no make-up gain)", 16, 27); compGroup.Controls.Add(compressorCheck);
        thresholdBox = AddNumber(compGroup, "Threshold dB", 16, 62, -60, 0, -18, 1);
        ratioBox = AddNumber(compGroup, "Ratio", 250, 62, 1, 10, 2, 1);
        kneeBox = AddNumber(compGroup, "Knee dB", 480, 62, 1, 12, 6, 1);
        attackBox = AddNumber(compGroup, "Attack ms", 16, 99, 1, 200, 15, 0);
        releaseBox = AddNumber(compGroup, "Release ms", 250, 99, 10, 1000, 125, 0);

        var loudGroup = Group("Loudness normalization", y, 110); scroll.Controls.Add(loudGroup); y += 118;
        loudnessCheck = CheckAt("Enable two-pass normalization", 16, 27); loudGroup.Controls.Add(loudnessCheck);
        monoTargetBox = AddNumber(loudGroup, "Mono target", 16, 64, -35, -5, -20, 1);
        stereoTargetBox = AddNumber(loudGroup, "Stereo target", 300, 64, -35, -5, -20, 1);

        var limitGroup = Group("Final limiter", y, 105); scroll.Controls.Add(limitGroup); y += 113;
        limiterCheck = CheckAt("Enable soft limiter", 16, 27); limitGroup.Controls.Add(limiterCheck);
        ceilingBox = AddNumber(limitGroup, "Ceiling dB", 250, 25, -9, 0, -1, 1);
        limitGroup.Controls.Add(LabelAt("10 ms attack/lookahead approximation; no automatic gain.", 16, 65));

        var exportGroup = Group("Custom export settings", y, 150); scroll.Controls.Add(exportGroup);
        exportGroup.Controls.Add(LabelAt("Format", 16, 31)); formatCombo = ComboAt(74, 27, 110); formatCombo.Items.AddRange(new object[] { "WAV", "MP3", "M4A/AAC", "OGG", "Opus", "WMA", "FLAC", "AIFF" }); exportGroup.Controls.Add(formatCombo);
        exportGroup.Controls.Add(LabelAt("Sample rate", 205, 31)); sampleRateCombo = ComboAt(285, 27, 105); sampleRateCombo.Items.AddRange(new object[] { "Preserve", "24000", "48000" }); exportGroup.Controls.Add(sampleRateCombo);
        exportGroup.Controls.Add(LabelAt("Channels", 414, 31)); channelsCombo = ComboAt(477, 27, 100); channelsCombo.Items.AddRange(new object[] { "Preserve", "Mono", "Stereo" }); exportGroup.Controls.Add(channelsCombo);
        exportGroup.Controls.Add(LabelAt("Mode", 16, 73)); bitrateModeCombo = ComboAt(74, 69, 110); bitrateModeCombo.Items.AddRange(new object[] { "VBR", "ABR", "CBR" }); exportGroup.Controls.Add(bitrateModeCombo);
        exportGroup.Controls.Add(LabelAt("Bitrate kbps", 205, 73)); bitrateBox = NumberAt(285, 69, 24, 320, 96, 0); exportGroup.Controls.Add(bitrateBox);
        exportGroup.Controls.Add(LabelAt("Same as Source preserves rate and channels. Spoken Opus uses mono/48 kHz/64 kbps VBR; Compatible MP3 uses mono/48 kHz/96 kbps ABR.", 16, 111, 870));
        scroll.Resize += delegate { ResizeAdvancedGroups(scroll); };
        ResizeAdvancedGroups(scroll);
    }

    private static void ResizeAdvancedGroups(Panel scroll)
    {
        int width = Math.Max(900, scroll.ClientSize.Width - 36);
        foreach (Control control in scroll.Controls)
            if (control is GroupBox) control.Width = width;
    }

    private void BuildPresets()
    {
        builtIns.Add(ProcessingPreset.Create("Audiobook", "LUFS", -20, -20, -1));
        builtIns.Add(ProcessingPreset.Create("General Listening", "LUFS", -19, -16, -1));
        builtIns.Add(ProcessingPreset.Create("ACX-style", "RMS", -20, -20, -3.5));
    }

    private string LocateFfmpeg()
    {
        string candidate = Path.GetFullPath(Path.Combine(appDirectory, "..", "python", "Lib", "site-packages", "imageio_ffmpeg", "binaries", "ffmpeg-win-x86_64-v7.1.exe"));
        if (File.Exists(candidate)) return candidate;
        string binaries = Path.GetFullPath(Path.Combine(appDirectory, "..", "python", "Lib", "site-packages", "imageio_ffmpeg", "binaries"));
        if (Directory.Exists(binaries))
        {
            string found = Directory.GetFiles(binaries, "ffmpeg*.exe").FirstOrDefault();
            if (found != null) return found;
        }
        return candidate;
    }

    private AppSettings LoadSettings()
    {
        try { if (File.Exists(settingsPath)) return json.Deserialize<AppSettings>(File.ReadAllText(settingsPath)); }
        catch { }
        return new AppSettings();
    }

    private void LoadUserPresets()
    {
        try
        {
            if (!File.Exists(presetsPath)) return;
            PresetFile file = json.Deserialize<PresetFile>(File.ReadAllText(presetsPath));
            if (file != null && file.Presets != null)
                foreach (ProcessingPreset preset in file.Presets) { preset.BuiltIn = false; userPresets.Add(preset); }
        }
        catch { }
    }

    private void SaveSettings()
    {
        settings.LastPreset = presetCombo.Text; settings.ExportProfile = exportProfileCombo.Text;
        settings.CustomFormat = formatCombo.Text; settings.SampleRate = sampleRateCombo.Text;
        settings.Channels = channelsCombo.Text; settings.BitrateMode = bitrateModeCombo.Text;
        settings.Bitrate = (int)bitrateBox.Value; settings.Workers = (int)workersBox.Value;
        settings.Suffix = string.IsNullOrWhiteSpace(suffixBox.Text) ? "_processed" : suffixBox.Text.Trim();
        settings.UseOutputFolder = outputFolderCheck.Checked; settings.OutputFolder = outputFolderBox.Text.Trim();
        AtomicWrite(settingsPath, json.Serialize(settings));
    }

    private void SaveUserPresets()
    {
        AtomicWrite(presetsPath, json.Serialize(new PresetFile { Version = 1, Presets = userPresets }));
    }

    private static void AtomicWrite(string path, string text)
    {
        string temporary = path + ".tmp"; File.WriteAllText(temporary, text, Encoding.UTF8);
        if (File.Exists(path)) { string backup = path + ".bak"; File.Replace(temporary, path, backup, true); try { File.Delete(backup); } catch { } }
        else File.Move(temporary, path);
    }

    private void LoadSettingsIntoUi()
    {
        workersBox.Value = Math.Max(1, Math.Min(8, settings.Workers)); suffixBox.Text = string.IsNullOrWhiteSpace(settings.Suffix) ? "_processed" : settings.Suffix;
        outputFolderCheck.Checked = settings.UseOutputFolder; outputFolderBox.Text = settings.OutputFolder ?? "";
        SelectCombo(exportProfileCombo, settings.ExportProfile, "Same as Source"); SelectCombo(formatCombo, settings.CustomFormat, "MP3");
        SelectCombo(sampleRateCombo, settings.SampleRate, "Preserve"); SelectCombo(channelsCombo, settings.Channels, "Preserve");
        SelectCombo(bitrateModeCombo, settings.BitrateMode, "VBR"); bitrateBox.Value = Math.Max(24, Math.Min(320, settings.Bitrate));
        ApplyExportProfile();
    }

    private void RefreshPresetCombo(string select)
    {
        presetCombo.Items.Clear(); foreach (ProcessingPreset preset in builtIns.Concat(userPresets)) presetCombo.Items.Add(preset.Name);
        int index = presetCombo.Items.IndexOf(select); presetCombo.SelectedIndex = index >= 0 ? index : 0;
    }

    private ProcessingPreset FindPreset(string name)
    {
        return builtIns.Concat(userPresets).FirstOrDefault(delegate(ProcessingPreset p) { return string.Equals(p.Name, name, StringComparison.OrdinalIgnoreCase); });
    }

    private void ApplySelectedPreset()
    {
        ProcessingPreset preset = FindPreset(presetCombo.Text); if (preset == null) return;
        masterBox.Value = DecimalValue(preset.MasterStrength, masterBox); tempoCheck.Checked = preset.TempoEnabled; tempoBox.Value = DecimalValue(preset.TempoPercent, tempoBox);
        eqCheck.Checked = preset.EqEnabled; eq120.Value = DecimalValue(preset.Eq120, eq120); eq200.Value = DecimalValue(preset.Eq200, eq200); eq500.Value = DecimalValue(preset.Eq500, eq500);
        eq3500.Value = DecimalValue(preset.Eq3500, eq3500); eq5000.Value = DecimalValue(preset.Eq5000, eq5000); eq10000.Value = DecimalValue(preset.Eq10000, eq10000);
        deEsserCheck.Checked = preset.DeEsserEnabled; deEsserBox.Value = DecimalValue(preset.DeEsserReduction, deEsserBox);
        compressorCheck.Checked = preset.CompressorEnabled; thresholdBox.Value = DecimalValue(preset.CompressorThreshold, thresholdBox); ratioBox.Value = DecimalValue(preset.CompressorRatio, ratioBox);
        kneeBox.Value = DecimalValue(preset.CompressorKnee, kneeBox); attackBox.Value = DecimalValue(preset.CompressorAttack, attackBox); releaseBox.Value = DecimalValue(preset.CompressorRelease, releaseBox);
        loudnessCheck.Checked = preset.LoudnessEnabled; monoTargetBox.Value = DecimalValue(preset.MonoTarget, monoTargetBox); stereoTargetBox.Value = DecimalValue(preset.StereoTarget, stereoTargetBox);
        limiterCheck.Checked = preset.LimiterEnabled; ceilingBox.Value = DecimalValue(preset.LimiterCeiling, ceilingBox);
    }

    private ProcessingPreset ReadPreset(string name, bool builtIn)
    {
        ProcessingPreset selected = FindPreset(presetCombo.Text);
        return new ProcessingPreset {
            Name = name, BuiltIn = builtIn, MasterStrength = (double)masterBox.Value,
            TempoEnabled = tempoCheck.Checked, TempoPercent = (double)tempoBox.Value,
            EqEnabled = eqCheck.Checked, Eq120 = (double)eq120.Value, Eq200 = (double)eq200.Value,
            Eq500 = (double)eq500.Value, Eq3500 = (double)eq3500.Value, Eq5000 = (double)eq5000.Value, Eq10000 = (double)eq10000.Value,
            DeEsserEnabled = deEsserCheck.Checked, DeEsserReduction = (double)deEsserBox.Value,
            CompressorEnabled = compressorCheck.Checked, CompressorThreshold = (double)thresholdBox.Value,
            CompressorRatio = (double)ratioBox.Value, CompressorKnee = (double)kneeBox.Value,
            CompressorAttack = (double)attackBox.Value, CompressorRelease = (double)releaseBox.Value,
            LoudnessEnabled = loudnessCheck.Checked, LoudnessMode = selected == null ? "LUFS" : selected.LoudnessMode,
            MonoTarget = (double)monoTargetBox.Value, StereoTarget = (double)stereoTargetBox.Value,
            LimiterEnabled = limiterCheck.Checked, LimiterCeiling = (double)ceilingBox.Value
        };
    }

    private void SavePreset()
    {
        ProcessingPreset current = FindPreset(presetCombo.Text);
        if (current != null && !current.BuiltIn)
        {
            int index = userPresets.IndexOf(current); userPresets[index] = ReadPreset(current.Name, false); SaveUserPresets(); return;
        }
        string name = AskName("Save Personal Preset", "Preset name", "My Preset"); if (name == null) return;
        if (FindPreset(name) != null) { MessageBox.Show("That preset name already exists.", Text); return; }
        userPresets.Add(ReadPreset(name, false)); SaveUserPresets(); RefreshPresetCombo(name);
    }

    private void DuplicatePreset()
    {
        string name = AskName("Duplicate Preset", "New preset name", presetCombo.Text + " Copy"); if (name == null) return;
        if (FindPreset(name) != null) { MessageBox.Show("That preset name already exists.", Text); return; }
        userPresets.Add(ReadPreset(name, false)); SaveUserPresets(); RefreshPresetCombo(name);
    }

    private void RenamePreset()
    {
        ProcessingPreset current = FindPreset(presetCombo.Text); if (current == null || current.BuiltIn) { MessageBox.Show("Built-in presets cannot be renamed.", Text); return; }
        string name = AskName("Rename Preset", "New preset name", current.Name); if (name == null || name == current.Name) return;
        if (FindPreset(name) != null) { MessageBox.Show("That preset name already exists.", Text); return; }
        current.Name = name; SaveUserPresets(); RefreshPresetCombo(name);
    }

    private void DeletePreset()
    {
        ProcessingPreset current = FindPreset(presetCombo.Text); if (current == null || current.BuiltIn) { MessageBox.Show("Built-in presets cannot be deleted.", Text); return; }
        if (MessageBox.Show("Delete personal preset '" + current.Name + "'?", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
        userPresets.Remove(current); SaveUserPresets(); RefreshPresetCombo("Audiobook");
    }

    private string AskName(string title, string label, string initial)
    {
        using (var dialog = new PromptDialog(title, label, initial)) return dialog.ShowDialog(this) == DialogResult.OK && dialog.Value.Length > 0 ? dialog.Value : null;
    }

    private void ApplyExportProfile()
    {
        bool custom = exportProfileCombo.Text == "Custom";
        if (exportProfileCombo.Text == "Spoken Opus") { SelectCombo(formatCombo, "Opus", "Opus"); SelectCombo(sampleRateCombo, "48000", "48000"); SelectCombo(channelsCombo, "Mono", "Mono"); SelectCombo(bitrateModeCombo, "VBR", "VBR"); bitrateBox.Value = 64; }
        else if (exportProfileCombo.Text == "Compatible MP3") { SelectCombo(formatCombo, "MP3", "MP3"); SelectCombo(sampleRateCombo, "48000", "48000"); SelectCombo(channelsCombo, "Mono", "Mono"); SelectCombo(bitrateModeCombo, "ABR", "ABR"); bitrateBox.Value = 96; }
        formatCombo.Enabled = custom; sampleRateCombo.Enabled = custom; channelsCombo.Enabled = custom; bitrateModeCombo.Enabled = custom; bitrateBox.Enabled = custom;
    }

    private ExportOptions ReadExportOptions()
    {
        var result = new ExportOptions { Profile = exportProfileCombo.Text, Format = formatCombo.Text, SampleRate = sampleRateCombo.Text, Channels = channelsCombo.Text, BitrateMode = bitrateModeCombo.Text, Bitrate = (int)bitrateBox.Value, Suffix = string.IsNullOrWhiteSpace(suffixBox.Text) ? "_processed" : suffixBox.Text.Trim(), UseOutputFolder = outputFolderCheck.Checked, OutputFolder = outputFolderBox.Text.Trim() };
        if (result.Profile == "Same as Source") { result.SampleRate = "Preserve"; result.Channels = "Preserve"; result.BitrateMode = "VBR"; result.Bitrate = 128; }
        else if (result.Profile == "Spoken Opus") { result.Format = "Opus"; result.SampleRate = "48000"; result.Channels = "Mono"; result.BitrateMode = "VBR"; result.Bitrate = 64; }
        else if (result.Profile == "Compatible MP3") { result.Format = "MP3"; result.SampleRate = "48000"; result.Channels = "Mono"; result.BitrateMode = "ABR"; result.Bitrate = 96; }
        return result;
    }

    private void AddFiles()
    {
        using (var dialog = new OpenFileDialog { Multiselect = true, Filter = "Audio files|*.wav;*.mp3;*.m4a;*.aac;*.mp4;*.m4b;*.ogg;*.opus;*.wma;*.flac;*.aif;*.aiff|All files|*.*" })
            if (dialog.ShowDialog(this) == DialogResult.OK) AddPaths(dialog.FileNames);
    }

    private void AddFolder()
    {
        using (var dialog = new FolderBrowserDialog { Description = "Add supported audio files from this folder and its subfolders" })
            if (dialog.ShowDialog(this) == DialogResult.OK) AddDirectory(dialog.SelectedPath);
    }

    private void AddDirectory(string directory)
    {
        try { AddPaths(Directory.EnumerateFiles(directory, "*", SearchOption.AllDirectories).Where(delegate(string p) { return Supported.Contains(Path.GetExtension(p)); })); }
        catch (Exception ex) { MessageBox.Show("Could not read folder:\r\n" + ex.Message, Text); }
    }

    private void AddPaths(IEnumerable<string> paths)
    {
        if (running) return; var existing = new HashSet<string>(jobs.Select(delegate(AudioJob j) { return j.Source; }), StringComparer.OrdinalIgnoreCase);
        foreach (string raw in paths)
        {
            string path; try { path = Path.GetFullPath(raw); } catch { continue; }
            if (!File.Exists(path) || !Supported.Contains(Path.GetExtension(path)) || !existing.Add(path)) continue;
            var item = new ListViewItem(path); item.SubItems.Add(Path.GetExtension(path).TrimStart('.').ToUpperInvariant()); item.SubItems.Add(""); item.SubItems.Add("Queued"); item.SubItems.Add("0%");
            var job = new AudioJob { Source = path, Status = "Queued", Progress = 0, Item = item }; item.Tag = job; jobs.Add(job); queue.Items.Add(item);
        }
        UpdateOverall();
    }

    private void RemoveSelected()
    {
        if (running) return; foreach (ListViewItem item in queue.SelectedItems.Cast<ListViewItem>().ToArray()) { jobs.Remove((AudioJob)item.Tag); queue.Items.Remove(item); } UpdateOverall();
    }

    private void RetryFailed()
    {
        if (running) return; foreach (AudioJob job in jobs.Where(delegate(AudioJob j) { return j.Status == "Failed" || j.Status == "Canceled"; })) { job.Status = "Queued"; job.Progress = 0; job.Error = null; UpdateJob(job); } UpdateOverall();
    }

    private void StartProcessing()
    {
        if (running || jobs.Count == 0) return;
        if (!File.Exists(ffmpegPath)) { MessageBox.Show("The bundled FFmpeg executable is missing:\r\n" + ffmpegPath, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); return; }
        ExportOptions export = ReadExportOptions();
        if (export.UseOutputFolder && (string.IsNullOrWhiteSpace(export.OutputFolder) || !Directory.Exists(export.OutputFolder))) { MessageBox.Show("Choose an existing output folder.", Text); return; }
        ProcessingPreset preset = ReadPreset(presetCombo.Text, false);
        List<AudioJob> pending = jobs.Where(delegate(AudioJob j) { return j.Status == "Queued" || j.Status == "Failed" || j.Status == "Canceled"; }).ToList();
        if (pending.Count == 0) return;
        PlanOutputs(pending, export);
        SaveSettings(); running = true; ToggleRunning(true); engine = new ProcessingEngine(ffmpegPath); engine.ResetCancellation();
        var work = new ConcurrentQueue<AudioJob>(pending); int count = Math.Min((int)workersBox.Value, pending.Count); var tasks = new Task[count];
        for (int i = 0; i < count; i++) tasks[i] = Task.Factory.StartNew(delegate { Worker(work, preset, export); }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
        Task.Factory.ContinueWhenAll(tasks, delegate { BeginInvoke(new Action(delegate { running = false; ToggleRunning(false); UpdateOverall(); })); });
    }

    private void Worker(ConcurrentQueue<AudioJob> work, ProcessingPreset preset, ExportOptions export)
    {
        AudioJob job;
        while (work.TryDequeue(out job))
        {
            if (!running) { job.Status = "Canceled"; SafeUpdate(job); continue; }
            try
            {
                job.Status = "Preparing"; job.Progress = 0; SafeUpdate(job);
                engine.ProcessFile(job, preset, export, delegate(int value, string status) { job.Progress = value; job.Status = status; SafeUpdate(job); });
                job.Status = "Completed"; job.Progress = 100;
            }
            catch (OperationCanceledException) { job.Status = "Canceled"; }
            catch (Exception ex) { job.Status = "Failed"; job.Error = ex.Message; }
            SafeUpdate(job);
        }
    }

    private void CancelProcessing()
    {
        if (!running) return; running = false; if (engine != null) engine.Cancel(); cancelButton.Enabled = false;
    }

    private void PlanOutputs(List<AudioJob> pending, ExportOptions export)
    {
        var reserved = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (AudioJob job in pending)
        {
            string extension = export.Profile == "Same as Source" ? Path.GetExtension(job.Source).ToLowerInvariant() : ExtensionFor(export.Format);
            string directory = export.UseOutputFolder ? export.OutputFolder : Path.GetDirectoryName(job.Source);
            string baseName = Path.GetFileNameWithoutExtension(job.Source) + export.Suffix;
            job.Output = OutputPlanner.Choose(directory, baseName, extension, reserved);
            job.Item.SubItems[2].Text = job.Output;
        }
    }

    private void SafeUpdate(AudioJob job)
    {
        try { BeginInvoke(new Action(delegate { UpdateJob(job); UpdateOverall(); })); } catch { }
    }

    private void UpdateJob(AudioJob job)
    {
        if (job.Item == null || job.Item.ListView == null) return;
        job.Item.SubItems[3].Text = job.Status; job.Item.SubItems[4].Text = job.Progress + "%";
        job.Item.ToolTipText = job.Error ?? "";
        job.Item.ForeColor = job.Status == "Failed" ? Color.Firebrick : (job.Status == "Completed" ? Color.DarkGreen : SystemColors.WindowText);
    }

    private void UpdateOverall()
    {
        int completed = jobs.Count(delegate(AudioJob j) { return j.Status == "Completed"; });
        int failed = jobs.Count(delegate(AudioJob j) { return j.Status == "Failed"; });
        int average = jobs.Count == 0 ? 0 : (int)jobs.Average(delegate(AudioJob j) { return j.Progress; });
        overallProgress.Value = Math.Max(0, Math.Min(100, average)); overallLabel.Text = completed + "/" + jobs.Count + " completed" + (failed > 0 ? ", " + failed + " failed" : "");
    }

    private void ToggleRunning(bool value)
    {
        startButton.Enabled = !value; retryButton.Enabled = !value; cancelButton.Enabled = value;
        presetCombo.Enabled = !value; workersBox.Enabled = !value; exportProfileCombo.Enabled = !value; outputFolderCheck.Enabled = !value; suffixBox.Enabled = !value;
    }

    private void OpenSelectedOutput()
    {
        AudioJob job = queue.SelectedItems.Count > 0 ? queue.SelectedItems[0].Tag as AudioJob : jobs.LastOrDefault(delegate(AudioJob j) { return j.Status == "Completed"; });
        if (job == null || string.IsNullOrWhiteSpace(job.Output)) return;
        string argument = File.Exists(job.Output) ? "/select," + QuoteArgument(job.Output) : QuoteArgument(Path.GetDirectoryName(job.Output));
        try { Process.Start("explorer.exe", argument); } catch { }
    }

    private void ChooseOutputFolder()
    {
        using (var dialog = new FolderBrowserDialog()) if (dialog.ShowDialog(this) == DialogResult.OK) { outputFolderBox.Text = dialog.SelectedPath; outputFolderCheck.Checked = true; }
    }

    private void OnDragEnter(object sender, DragEventArgs e) { if (e.Data.GetDataPresent(DataFormats.FileDrop)) e.Effect = DragDropEffects.Copy; }
    private void OnDragDrop(object sender, DragEventArgs e)
    {
        if (running) return; string[] paths = (string[])e.Data.GetData(DataFormats.FileDrop);
        foreach (string path in paths) { if (Directory.Exists(path)) AddDirectory(path); else AddPaths(new[] { path }); }
    }

    private void OnFormClosing(object sender, FormClosingEventArgs e)
    {
        if (running)
        {
            if (MessageBox.Show("Cancel active processing and exit?", Text, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) { e.Cancel = true; return; }
            CancelProcessing();
        }
        try { SaveSettings(); } catch { }
    }

    private static string ExtensionFor(string format)
    {
        switch (format) { case "WAV": return ".wav"; case "MP3": return ".mp3"; case "M4A/AAC": return ".m4a"; case "OGG": return ".ogg"; case "Opus": return ".opus"; case "WMA": return ".wma"; case "FLAC": return ".flac"; case "AIFF": return ".aiff"; default: return ".wav"; }
    }

    private static decimal DecimalValue(double value, NumericUpDown box) { return Math.Max(box.Minimum, Math.Min(box.Maximum, (decimal)value)); }
    private static void SelectCombo(ComboBox combo, string value, string fallback) { int index = combo.Items.IndexOf(value); combo.SelectedIndex = index >= 0 ? index : combo.Items.IndexOf(fallback); }
    private static string QuoteArgument(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }

    private void ResizeQueueColumns()
    {
        if (queue == null || queue.Columns.Count < 5 || queue.ClientSize.Width <= 0) return;
        int available = Math.Max(500, queue.ClientSize.Width - SystemInformation.VerticalScrollBarWidth - 4);
        int flexible = Math.Max(440, available - 65 - 150 - 80);
        queue.Columns[0].Width = (int)(flexible * 0.55);
        queue.Columns[1].Width = 65;
        queue.Columns[2].Width = flexible - queue.Columns[0].Width;
        queue.Columns[3].Width = 150;
        queue.Columns[4].Width = 80;
    }

    private static FlowLayoutPanel FlowRow()
    {
        return new FlowLayoutPanel {
            Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            FlowDirection = FlowDirection.LeftToRight, WrapContents = true, Margin = Padding.Empty, Padding = Padding.Empty
        };
    }

    private static Button FlowButton(string text, Action action, int width)
    {
        var button = new Button { Text = text, AutoSize = false, Size = new Size(width, 30), Margin = new Padding(3) };
        button.Click += delegate { action(); }; return button;
    }

    private static Label FlowLabel(string text, int width)
    {
        return new Label {
            Text = text, AutoSize = false, Size = new Size(width, 27), TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = true, Margin = new Padding(3, 4, 3, 3)
        };
    }

    private static ComboBox FlowCombo(int width)
    {
        return new ComboBox {
            Width = width, DropDownStyle = ComboBoxStyle.DropDownList, Margin = new Padding(3, 4, 8, 3)
        };
    }

    private static NumericUpDown FlowNumber(int width, decimal min, decimal max, decimal value, int decimals)
    {
        return new NumericUpDown {
            AutoSize = false, Size = new Size(width, 26), Minimum = min, Maximum = max, Value = value,
            DecimalPlaces = decimals, Increment = decimals == 0 ? 1 : 0.1M, Margin = new Padding(3, 4, 5, 3)
        };
    }

    private static Button ButtonAt(string text, int x, int y, Action action, int width)
    {
        var button = new Button { Text = text, Location = new Point(x, y), Size = new Size(width, 30) }; button.Click += delegate { action(); }; return button;
    }
    private static Button ButtonAt(string text, int x, int y, Action action) { return ButtonAt(text, x, y, action, 84); }
    private static Label LabelAt(string text, int x, int y) { return new Label { Text = text, Location = new Point(x, y), AutoSize = true }; }
    private static Label LabelAt(string text, int x, int y, int width) { return new Label { Text = text, Location = new Point(x, y), Size = new Size(width, 25), AutoEllipsis = true }; }
    private static ComboBox ComboAt(int x, int y, int width) { return new ComboBox { Location = new Point(x, y), Width = width, DropDownStyle = ComboBoxStyle.DropDownList }; }
    private static NumericUpDown NumberAt(int x, int y, decimal min, decimal max, decimal value, int decimals) { return new NumericUpDown { Location = new Point(x, y), Width = 62, Minimum = min, Maximum = max, Value = value, DecimalPlaces = decimals, Increment = decimals == 0 ? 1 : 0.1M }; }
    private static CheckBox CheckAt(string text, int x, int y) { return new CheckBox { Text = text, Location = new Point(x, y), AutoSize = true }; }
    private static GroupBox Group(string text, int y, int height) { return new GroupBox { Text = text, Location = new Point(18, y), Size = new Size(900, height), Anchor = AnchorStyles.Top | AnchorStyles.Left }; }
    private static NumericUpDown AddNumber(Control parent, string label, int x, int y, decimal min, decimal max, decimal value, int decimals) { parent.Controls.Add(LabelAt(label, x, y + 4, 90)); NumericUpDown box = NumberAt(x + 95, y, min, max, value, decimals); parent.Controls.Add(box); return box; }
}

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new AudioPostProcessorForm());
    }
}
