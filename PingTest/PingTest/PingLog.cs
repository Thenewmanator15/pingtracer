using System;
using System.Collections.Generic;
using System.Text;
using System.Net.NetworkInformation;

namespace PingTracer
{
	public class PingLog
	{
		/// <summary>
		/// When the ping was sent.
		/// </summary>
		public DateTime startTime;
		/// <summary>
		/// The round trip time reported by the operating system, in whole milliseconds.
		/// </summary>
		public short pingTime;
		public IPStatus result;
		/// <summary>
		/// Microseconds from sending the ping until this program was told the result, or 0 if that was not measured.  This is finer than <see cref="pingTime"/> but includes the delay before the program was notified.
		/// </summary>
		public int measuredMicroseconds;
		/// <summary>
		/// True if the ping succeeded but the data in the reply was not the data that was sent.
		/// </summary>
		public bool dataMismatch;
		/// <summary>
		/// Size in bytes of the payload that was sent, or -1 if that was not recorded.
		/// </summary>
		public int payloadSize = -1;
		/// <summary>
		/// What the payload was filled with, as a <see cref="PingTracer.Util.PingPayloadPattern"/> value.
		/// </summary>
		public byte payloadPattern;
		/// <summary>
		/// The seed a random payload was generated from.  Together with <see cref="payloadSize"/> and <see cref="payloadPattern"/> this is enough to rebuild exactly what was sent.
		/// </summary>
		public int payloadSeed;
		/// <summary>
		/// The data returned in the reply.  Only kept when it differed from what was sent (see <see cref="dataMismatch"/>); otherwise null.
		/// </summary>
		public byte[] receivedData;
		public PingLog(DateTime startTime, short pingTime, IPStatus result)
		{
			this.startTime = startTime;
			this.pingTime = pingTime;
			this.result = result;
		}
		public PingLog(DateTime startTime, short pingTime, IPStatus result, int measuredMicroseconds) : this(startTime, pingTime, result)
		{
			this.measuredMicroseconds = measuredMicroseconds;
		}
		/// <summary>
		/// When this program was told the result of the ping, or null if that was not measured.
		/// </summary>
		public DateTime? ReceivedTime
		{
			get
			{
				if (measuredMicroseconds <= 0)
					return null;
				return startTime.AddTicks(measuredMicroseconds * 10L);
			}
		}
	}
}
