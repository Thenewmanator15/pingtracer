using System;

namespace PingTracer
{
	/// <summary>
	/// A run of consecutive pings to one host which got no successful response.
	/// </summary>
	public class Outage
	{
		/// <summary>Display name of the host that stopped responding.</summary>
		public string Host;
		/// <summary>When the first lost ping was sent.</summary>
		public DateTime Start;
		/// <summary>When the first ping to succeed afterwards was sent.  For an ongoing outage, when the most recent lost ping was sent.</summary>
		public DateTime End;
		/// <summary>How many pings in a row were lost.</summary>
		public int LostPings;
		/// <summary>True if no ping has succeeded since the outage began.</summary>
		public bool Ongoing;

		public TimeSpan Duration { get { return End - Start; } }

		/// <summary>
		/// Formats a duration the way a person would say it, e.g. "42 s" or "3 min 05 s".
		/// </summary>
		public static string FormatDuration(TimeSpan duration)
		{
			if (duration.TotalSeconds < 60)
				return duration.TotalSeconds.ToString(duration.TotalSeconds < 10 ? "0.#" : "0") + " s";
			if (duration.TotalHours < 1)
				return duration.Minutes + " min " + duration.Seconds.ToString("00") + " s";
			return (int)duration.TotalHours + " h " + duration.Minutes.ToString("00") + " min";
		}
	}
}
