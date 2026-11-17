using System;
using System.Drawing;
using System.Net;
using System.Net.Sockets;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace PingTracer
{
	/// <summary>
	/// A window for running the one-off measurements in <see cref="NetworkTests"/> against a host.
	/// </summary>
	public class NetworkTestsForm : Form
	{
		private readonly int pingTimeoutMs;
		private readonly bool preferIpv4;
		private readonly TextBox txtHost = new TextBox();
		private readonly NumericUpDown nudPort = new NumericUpDown();
		private readonly Button btnLargestPacket = new Button();
		private readonly Button btnConnect = new Button();
		private readonly TextBox txtOutput = new TextBox();

		/// <param name="host">Host to pre-fill, or null.</param>
		/// <param name="pingTimeoutMs">Longest time to wait for any single response.</param>
		/// <param name="preferIpv4">Whether to prefer IPv4 addresses when a name has both kinds.</param>
		public NetworkTestsForm(string host, int pingTimeoutMs, bool preferIpv4)
		{
			this.pingTimeoutMs = pingTimeoutMs;
			this.preferIpv4 = preferIpv4;

			Text = "Network Tests - PingTracer";
			ClientSize = new Size(560, 360);
			MinimumSize = new Size(480, 240);
			StartPosition = FormStartPosition.CenterParent;

			Label lblHost = new Label { Text = "Host:", AutoSize = true, Location = new Point(12, 15) };
			txtHost.Location = new Point(50, 12);
			txtHost.Size = new Size(330, 20);
			txtHost.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
			txtHost.Text = host ?? "";

			Label lblPort = new Label { Text = "Port:", AutoSize = true, Location = new Point(392, 15), Anchor = AnchorStyles.Top | AnchorStyles.Right };
			nudPort.Location = new Point(428, 12);
			nudPort.Size = new Size(60, 20);
			nudPort.Anchor = AnchorStyles.Top | AnchorStyles.Right;
			nudPort.Minimum = 1;
			nudPort.Maximum = 65535;
			nudPort.Value = 443;

			btnLargestPacket.Text = "Biggest packet test";
			btnLargestPacket.Location = new Point(12, 42);
			btnLargestPacket.Size = new Size(150, 25);
			btnLargestPacket.Click += btnLargestPacket_Click;

			btnConnect.Text = "Lookup and connect time";
			btnConnect.Location = new Point(170, 42);
			btnConnect.Size = new Size(170, 25);
			btnConnect.Click += btnConnect_Click;

			txtOutput.Location = new Point(12, 76);
			txtOutput.Size = new Size(536, 272);
			txtOutput.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
			txtOutput.Multiline = true;
			txtOutput.ReadOnly = true;
			txtOutput.ScrollBars = ScrollBars.Vertical;
			txtOutput.Font = new Font(FontFamily.GenericMonospace, 9f);
			txtOutput.BackColor = SystemColors.Window;

			Controls.AddRange(new Control[] { lblHost, txtHost, lblPort, nudPort, btnLargestPacket, btnConnect, txtOutput });
		}

		private async void btnLargestPacket_Click(object sender, EventArgs e)
		{
			string host = txtHost.Text.Trim();
			if (host.Length == 0)
				return;
			await RunTest(() =>
			{
				IPAddress target = ResolveIpv4(host);
				if (target == null)
					return "Biggest packet test: " + host + Environment.NewLine + "  No IPv4 address was found for this host. The test only supports IPv4." + Environment.NewLine;
				Append("Biggest packet test: trying sizes against " + target + " ..." + Environment.NewLine);
				return NetworkTests.FindLargestPacket(target, pingTimeoutMs, line => Append(line + Environment.NewLine)).ToReport(host);
			});
		}

		private async void btnConnect_Click(object sender, EventArgs e)
		{
			string host = txtHost.Text.Trim();
			if (host.Length == 0)
				return;
			int port = (int)nudPort.Value;
			await RunTest(() => NetworkTests.MeasureLookupAndConnect(host, port, pingTimeoutMs, 5, preferIpv4).ToReport());
		}

		/// <summary>
		/// Runs a test on a background thread with the buttons disabled, then shows its report.
		/// </summary>
		private async Task RunTest(Func<string> test)
		{
			btnLargestPacket.Enabled = btnConnect.Enabled = false;
			try
			{
				string report;
				try
				{
					report = await Task.Run(test);
				}
				catch (Exception ex)
				{
					report = "The test failed: " + ex.Message + Environment.NewLine;
				}
				Append(report + Environment.NewLine);
			}
			finally
			{
				if (!IsDisposed)
					btnLargestPacket.Enabled = btnConnect.Enabled = true;
			}
		}

		private static IPAddress ResolveIpv4(string host)
		{
			IPAddress address;
			if (IPAddress.TryParse(host, out address))
				return address.AddressFamily == AddressFamily.InterNetwork ? address : null;
			foreach (IPAddress candidate in Dns.GetHostAddresses(host))
			{
				if (candidate.AddressFamily == AddressFamily.InterNetwork)
					return candidate;
			}
			return null;
		}

		/// <summary>
		/// Appends text to the output box.  Safe to call from any thread, and after the window has closed.
		/// </summary>
		private void Append(string text)
		{
			if (IsDisposed || !IsHandleCreated)
				return;
			if (InvokeRequired)
			{
				try
				{
					BeginInvoke((Action<string>)Append, text);
				}
				catch (InvalidOperationException) { } // The window closed while the test was running.
				return;
			}
			txtOutput.AppendText(text);
		}
	}
}
