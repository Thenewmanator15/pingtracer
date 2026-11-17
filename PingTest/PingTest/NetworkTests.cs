using System;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace PingTracer
{
	/// <summary>
	/// One-off network measurements which complement continuous ping monitoring.
	/// </summary>
	public static class NetworkTests
	{
		/// <summary>
		/// Bytes of IPv4 header (20) and ICMP header (8) added to a ping payload.
		/// </summary>
		public const int Ipv4IcmpOverheadBytes = 28;
		/// <summary>
		/// The largest ping payload which fits a standard 1500 byte Ethernet MTU.
		/// </summary>
		public const int StandardMaxPayloadBytes = 1500 - Ipv4IcmpOverheadBytes;
		private const int baselinePayloadBytes = 32;

		#region Largest packet

		public sealed class LargestPacketResult
		{
			/// <summary>False if the host did not answer an ordinary small ping, in which case nothing else is meaningful.</summary>
			public bool Reachable;
			/// <summary>The largest payload which was answered with the Don't Fragment flag set.</summary>
			public int LargestPayloadBytes;
			/// <summary>The size limit that was searched up to.</summary>
			public int SearchLimitBytes;
			/// <summary>True if at least one oversized ping was rejected with an explicit "packet too big" answer.</summary>
			public bool SawPacketTooBig;
			/// <summary>True if at least one oversized ping simply went unanswered.</summary>
			public bool SawSilentDrop;
			/// <summary>Response time of the initial small ping, in milliseconds.</summary>
			public long BaselineMs;
			/// <summary>How many pings were sent in total.</summary>
			public int PingsSent;

			/// <summary>The path MTU implied by <see cref="LargestPayloadBytes"/>.</summary>
			public int Mtu { get { return LargestPayloadBytes + Ipv4IcmpOverheadBytes; } }

			public string ToReport(string host)
			{
				StringBuilder sb = new StringBuilder();
				sb.AppendLine("Biggest packet test: " + host);
				if (!Reachable)
				{
					sb.AppendLine("  The host did not answer an ordinary ping, so the test could not run.");
					return sb.ToString();
				}
				sb.AppendLine("  Largest packet that got through whole: " + Mtu + " bytes (" + LargestPayloadBytes + " of data + " + Ipv4IcmpOverheadBytes + " of headers)");
				if (LargestPayloadBytes >= SearchLimitBytes)
					sb.AppendLine("  That is the full standard size. Nothing on the path is shrinking packets.");
				else
				{
					sb.AppendLine("  That is " + (SearchLimitBytes - LargestPayloadBytes) + " bytes short of the standard " + (SearchLimitBytes + Ipv4IcmpOverheadBytes) + ". Something on the path (VPN, PPPoE, tunnel) has a smaller limit.");
					if (SawSilentDrop && !SawPacketTooBig)
						sb.AppendLine("  Oversized packets vanished without any \"too big\" reply. Connections that rely on that reply can stall on this path.");
					else if (SawPacketTooBig)
						sb.AppendLine("  Oversized packets were rejected with a proper \"too big\" reply, so connections can adapt.");
				}
				sb.AppendLine("  (" + PingsSent + " pings sent, small ping took " + BaselineMs + " ms)");
				return sb.ToString();
			}
		}

		/// <summary>
		/// Finds the largest value in [knownGood, max] for which <paramref name="fits"/> returns true, assuming every value up to that point fits and every value beyond it does not.
		/// </summary>
		/// <param name="knownGood">A value already known to fit.</param>
		/// <param name="max">The largest value worth trying.</param>
		/// <param name="fits">Returns true if the value fits.</param>
		public static int SearchLargest(int knownGood, int max, Func<int, bool> fits)
		{
			if (knownGood >= max || fits(max))
				return Math.Max(knownGood, max);
			int lo = knownGood; // fits
			int hi = max; // does not fit
			while (hi - lo > 1)
			{
				int mid = lo + (hi - lo) / 2;
				if (fits(mid))
					lo = mid;
				else
					hi = mid;
			}
			return lo;
		}

		/// <summary>
		/// Finds the largest ping which reaches an IPv4 host without being fragmented (the path MTU), by sending pings of different sizes with the Don't Fragment flag set.
		/// </summary>
		/// <param name="target">IPv4 address to test.</param>
		/// <param name="maxTimeoutMs">Upper bound on how long to wait for each response.</param>
		/// <param name="progress">Optional callback which receives a line of text after each step.</param>
		/// <param name="maxPayloadBytes">The largest payload to try.</param>
		public static LargestPacketResult FindLargestPacket(IPAddress target, int maxTimeoutMs, Action<string> progress = null, int maxPayloadBytes = StandardMaxPayloadBytes)
		{
			if (target.AddressFamily != AddressFamily.InterNetwork)
				throw new NotSupportedException("The biggest packet test only supports IPv4 addresses.");

			LargestPacketResult result = new LargestPacketResult();
			result.SearchLimitBytes = maxPayloadBytes;
			using (Ping ping = new Ping())
			{
				PingOptions options = new PingOptions(64, true);

				// An ordinary small ping proves the host answers at all and tells us how long is worth waiting.
				for (int attempt = 0; attempt < 3 && !result.Reachable; attempt++)
				{
					PingReply reply = TrySend(ping, target, maxTimeoutMs, baselinePayloadBytes, options);
					result.PingsSent++;
					if (reply != null && reply.Status == IPStatus.Success)
					{
						result.Reachable = true;
						result.BaselineMs = reply.RoundtripTime;
					}
				}
				if (!result.Reachable)
					return result;

				int timeoutMs = (int)Math.Max(1000, Math.Min(maxTimeoutMs, result.BaselineMs * 4 + 500));

				result.LargestPayloadBytes = SearchLargest(baselinePayloadBytes, maxPayloadBytes, size =>
				{
					bool fits = false;
					bool tooBig = false;
					// A lone lost ping must not be mistaken for a size limit, so an unanswered size is tried twice.
					for (int attempt = 0; attempt < 2 && !fits && !tooBig; attempt++)
					{
						PingReply reply = TrySend(ping, target, timeoutMs, size, options);
						result.PingsSent++;
						if (reply != null && reply.Status == IPStatus.Success)
							fits = true;
						else if (reply != null && reply.Status == IPStatus.PacketTooBig)
							tooBig = true;
					}
					if (tooBig)
						result.SawPacketTooBig = true;
					else if (!fits)
						result.SawSilentDrop = true;
					if (progress != null)
						progress("  " + (size + Ipv4IcmpOverheadBytes) + " bytes: " + (fits ? "got through" : tooBig ? "too big" : "no reply"));
					return fits;
				});
			}
			return result;
		}

		private static PingReply TrySend(Ping ping, IPAddress target, int timeoutMs, int payloadBytes, PingOptions options)
		{
			try
			{
				return ping.Send(target, timeoutMs, new byte[payloadBytes], options);
			}
			catch (PingException)
			{
				return null;
			}
		}

		#endregion

		#region Lookup and connect time

		public sealed class ConnectResult
		{
			public string Host;
			public int Port;
			/// <summary>True if <see cref="Host"/> was already an IP address, so no lookup was needed.</summary>
			public bool HostIsAddress;
			/// <summary>Null if the name lookup succeeded.</summary>
			public string LookupError;
			/// <summary>Time taken by the first name lookup.</summary>
			public double FirstLookupMs;
			/// <summary>Time taken by an immediate second lookup, which is normally answered from the local cache.</summary>
			public double RepeatLookupMs;
			public IPAddress Address;
			public int Attempts;
			public int Connected;
			public double MinMs, AvgMs, MaxMs;
			/// <summary>The most recent connection failure, or null if every attempt connected.</summary>
			public string ConnectError;

			public string ToReport()
			{
				CultureInfo c = CultureInfo.InvariantCulture;
				StringBuilder sb = new StringBuilder();
				sb.AppendLine("Lookup and connect time: " + Host + " port " + Port);
				if (HostIsAddress)
					sb.AppendLine("  Name lookup: not needed, an address was given.");
				else if (LookupError != null)
				{
					sb.AppendLine("  Name lookup FAILED: " + LookupError);
					return sb.ToString();
				}
				else
				{
					sb.AppendLine("  Name lookup: " + FirstLookupMs.ToString("0.0", c) + " ms, found " + Address);
					sb.AppendLine("  Repeat lookup (from cache): " + RepeatLookupMs.ToString("0.0", c) + " ms");
				}
				if (Connected > 0)
					sb.AppendLine("  Connect: " + Connected + " of " + Attempts + " worked.  Fastest " + MinMs.ToString("0.0", c) + " ms, average " + AvgMs.ToString("0.0", c) + " ms, slowest " + MaxMs.ToString("0.0", c) + " ms");
				else
					sb.AppendLine("  Connect: 0 of " + Attempts + " worked.");
				if (ConnectError != null)
					sb.AppendLine("  Last failure: " + ConnectError);
				return sb.ToString();
			}
		}

		/// <summary>
		/// Times the DNS lookup for a host name and how long it takes to open a TCP connection to it.
		/// </summary>
		/// <param name="host">Host name or IP address.</param>
		/// <param name="port">TCP port to connect to.</param>
		/// <param name="timeoutMs">How long to wait for each connection.</param>
		/// <param name="attempts">How many connections to open.</param>
		/// <param name="preferIpv4">If true and the name has both IPv4 and IPv6 addresses, the IPv4 address is used.</param>
		public static ConnectResult MeasureLookupAndConnect(string host, int port, int timeoutMs, int attempts = 5, bool preferIpv4 = true)
		{
			ConnectResult result = new ConnectResult();
			result.Host = host;
			result.Port = port;
			result.Attempts = attempts;

			IPAddress address;
			if (IPAddress.TryParse(host, out address))
				result.HostIsAddress = true;
			else
			{
				try
				{
					Stopwatch lookupTimer = Stopwatch.StartNew();
					IPAddress[] addresses = Dns.GetHostAddresses(host);
					result.FirstLookupMs = lookupTimer.Elapsed.TotalMilliseconds;
					lookupTimer.Restart();
					Dns.GetHostAddresses(host);
					result.RepeatLookupMs = lookupTimer.Elapsed.TotalMilliseconds;

					address = null;
					AddressFamily preferred = preferIpv4 ? AddressFamily.InterNetwork : AddressFamily.InterNetworkV6;
					foreach (IPAddress candidate in addresses)
					{
						if (address == null || (candidate.AddressFamily == preferred && address.AddressFamily != preferred))
							address = candidate;
					}
					if (address == null)
					{
						result.LookupError = "The name has no addresses.";
						return result;
					}
				}
				catch (SocketException ex)
				{
					result.LookupError = ex.Message;
					return result;
				}
			}
			result.Address = address;

			double sum = 0;
			result.MinMs = double.MaxValue;
			Stopwatch timer = new Stopwatch();
			for (int i = 0; i < attempts; i++)
			{
				if (i > 0)
					Thread.Sleep(100);
				using (Socket socket = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp))
				{
					try
					{
						timer.Restart();
						IAsyncResult pending = socket.BeginConnect(address, port, null, null);
						if (!pending.AsyncWaitHandle.WaitOne(timeoutMs))
						{
							result.ConnectError = "No answer within " + timeoutMs + " ms.";
							continue; // Disposing the socket abandons the attempt.
						}
						socket.EndConnect(pending);
						double ms = timer.Elapsed.TotalMilliseconds;
						result.Connected++;
						sum += ms;
						if (ms < result.MinMs)
							result.MinMs = ms;
						if (ms > result.MaxMs)
							result.MaxMs = ms;
					}
					catch (SocketException ex)
					{
						result.ConnectError = ex.Message;
					}
				}
			}
			if (result.Connected > 0)
				result.AvgMs = sum / result.Connected;
			else
				result.MinMs = 0;
			return result;
		}

		#endregion
	}
}
