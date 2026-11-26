using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace PingTracer
{
	/// <summary>
	/// Lists the outages (runs of lost pings) found in the pings currently buffered by every graph.
	/// </summary>
	public class OutagesForm : Form
	{
		private const string timeFormat = "yyyy-MM-dd HH:mm:ss";
		private readonly NumericUpDown nudMinLost = new NumericUpDown();
		private readonly Button btnRefresh = new Button();
		private readonly Button btnSave = new Button();
		private readonly ListView list = new ListView();
		private readonly Label lblSummary = new Label();
		private List<Outage> outages = new List<Outage>();

		public OutagesForm()
		{
			Text = "Outages - PingTracer";
			ClientSize = new Size(640, 380);
			MinimumSize = new Size(520, 240);
			StartPosition = FormStartPosition.CenterParent;

			Label lblMin = new Label { Text = "Count as an outage after this many lost pings in a row:", AutoSize = true, Location = new Point(12, 15) };
			nudMinLost.Location = new Point(292, 12);
			nudMinLost.Size = new Size(50, 20);
			nudMinLost.Minimum = 1;
			nudMinLost.Maximum = 1000;
			nudMinLost.Value = 2;
			nudMinLost.ValueChanged += (sender, e) => RefreshOutages();

			btnRefresh.Text = "Refresh";
			btnRefresh.Location = new Point(360, 10);
			btnRefresh.Size = new Size(75, 24);
			btnRefresh.Click += (sender, e) => RefreshOutages();

			btnSave.Text = "Save as CSV...";
			btnSave.Location = new Point(441, 10);
			btnSave.Size = new Size(100, 24);
			btnSave.Click += btnSave_Click;

			list.Location = new Point(12, 44);
			list.Size = new Size(616, 284);
			list.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
			list.View = View.Details;
			list.FullRowSelect = true;
			list.GridLines = true;
			list.Columns.Add("Host", 210);
			list.Columns.Add("Started", 130);
			list.Columns.Add("Ended", 130);
			list.Columns.Add("Lasted", 75, HorizontalAlignment.Right);
			list.Columns.Add("Pings lost", 65, HorizontalAlignment.Right);

			lblSummary.Location = new Point(12, 334);
			lblSummary.Size = new Size(616, 38);
			lblSummary.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;

			Controls.AddRange(new Control[] { lblMin, nudMinLost, btnRefresh, btnSave, list, lblSummary });
			RefreshOutages();
		}

		/// <summary>
		/// Re-reads every graph's buffered pings and rebuilds the list.
		/// </summary>
		public void RefreshOutages()
		{
			int minLost = (int)nudMinLost.Value;
			int completed = 0, lost = 0;
			outages = new List<Outage>();
			foreach (PingGraphControl graph in MainForm.pingGraphs.Values)
			{
				int graphCompleted, graphLost;
				outages.AddRange(graph.GetOutages(minLost, out graphCompleted, out graphLost));
				completed += graphCompleted;
				lost += graphLost;
			}
			outages.Sort((a, b) => a.Start.CompareTo(b.Start));

			TimeSpan totalDown = TimeSpan.Zero, longest = TimeSpan.Zero;
			list.BeginUpdate();
			list.Items.Clear();
			foreach (Outage o in outages)
			{
				ListViewItem item = new ListViewItem(o.Host);
				item.SubItems.Add(o.Start.ToString(timeFormat));
				item.SubItems.Add(o.Ongoing ? "still down" : o.End.ToString(timeFormat));
				item.SubItems.Add((o.Ongoing ? "> " : "") + Outage.FormatDuration(o.Duration));
				item.SubItems.Add(o.LostPings.ToString());
				list.Items.Add(item);
				totalDown += o.Duration;
				if (o.Duration > longest)
					longest = o.Duration;
			}
			list.EndUpdate();

			if (completed == 0)
				lblSummary.Text = "No ping results yet.";
			else
			{
				lblSummary.Text = outages.Count + (outages.Count == 1 ? " outage" : " outages")
					+ (outages.Count > 0 ? ", " + Outage.FormatDuration(totalDown) + " down in total, longest " + Outage.FormatDuration(longest) : "")
					+ "." + Environment.NewLine
					+ lost + " of " + completed + " pings lost (" + (100.0 * lost / completed).ToString("0.00") + "%), counted across all graphs in memory.";
			}
			btnSave.Enabled = outages.Count > 0;
		}

		private void btnSave_Click(object sender, EventArgs e)
		{
			using (SaveFileDialog dialog = new SaveFileDialog())
			{
				dialog.Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*";
				dialog.FileName = "PingTracer outages " + DateTime.Now.ToString("yyyy'-'MM'-'dd HH'-'mm'-'ss") + ".csv";
				if (dialog.ShowDialog(this) != DialogResult.OK)
					return;
				try
				{
					WriteCsv(dialog.FileName, outages);
				}
				catch (Exception ex)
				{
					MessageBox.Show("Unable to save the outage list: " + ex.Message);
				}
			}
		}

		/// <summary>
		/// Writes outages to a CSV file (Host, Started, Ended, Seconds, PingsLost, Ongoing).
		/// </summary>
		public static void WriteCsv(string path, List<Outage> outages)
		{
			CultureInfo invariant = CultureInfo.InvariantCulture;
			using (StreamWriter writer = new StreamWriter(path, false, new UTF8Encoding(true)))
			{
				writer.WriteLine("Host,Started,Ended,Seconds,PingsLost,Ongoing");
				foreach (Outage o in outages)
				{
					writer.Write('"');
					writer.Write(o.Host.Replace("\"", "\"\""));
					writer.Write("\",");
					writer.Write(o.Start.ToString(timeFormat, invariant));
					writer.Write(',');
					writer.Write(o.End.ToString(timeFormat, invariant));
					writer.Write(',');
					writer.Write(o.Duration.TotalSeconds.ToString("0.###", invariant));
					writer.Write(',');
					writer.Write(o.LostPings.ToString(invariant));
					writer.Write(',');
					writer.WriteLine(o.Ongoing ? "yes" : "no");
				}
			}
		}
	}
}
