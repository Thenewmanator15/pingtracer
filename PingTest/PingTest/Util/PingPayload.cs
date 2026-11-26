using System;

namespace PingTracer.Util
{
	/// <summary>
	/// What the data area of each ping is filled with.  The values are stored in settings.cfg, so do not renumber them.
	/// </summary>
	public enum PingPayloadPattern
	{
		/// <summary>Every byte 0x00.  Compresses to almost nothing on links which compress.</summary>
		Zeros = 0,
		/// <summary>Every byte 0xFF.</summary>
		Ones = 1,
		/// <summary>Bytes alternate 0x55, 0xAA, so every bit differs from its neighbours.</summary>
		Alternating = 2,
		/// <summary>Fresh random bytes for every round of pings.  Cannot be compressed, so the full size crosses the link.</summary>
		Random = 3
	}

	/// <summary>
	/// Builds ping payloads and checks that a reply returned the payload unchanged.
	/// </summary>
	public static class PingPayload
	{
		/// <summary>
		/// Display names for <see cref="PingPayloadPattern"/>, in value order.
		/// </summary>
		public static readonly string[] PatternNames = new string[] { "Zeros", "Ones (0xFF)", "Alternating (0x55 0xAA)", "Random" };

		/// <summary>
		/// Converts a stored setting to a pattern, treating unknown values as <see cref="PingPayloadPattern.Zeros"/>.
		/// </summary>
		public static PingPayloadPattern FromSetting(int value)
		{
			return value >= 0 && value < PatternNames.Length ? (PingPayloadPattern)value : PingPayloadPattern.Zeros;
		}

		/// <summary>
		/// Returns true if a payload of this pattern has different content every time it is created, so it cannot be reused between rounds of pings.
		/// </summary>
		public static bool ChangesEveryRound(PingPayloadPattern pattern)
		{
			return pattern == PingPayloadPattern.Random;
		}

		/// <summary>
		/// Creates a payload.
		/// </summary>
		/// <param name="pattern">What to fill it with.</param>
		/// <param name="size">Size in bytes.</param>
		/// <param name="random">Source of bytes for <see cref="PingPayloadPattern.Random"/>.</param>
		public static byte[] Create(PingPayloadPattern pattern, int size, Random random)
		{
			byte[] payload = new byte[Math.Max(0, size)];
			switch (pattern)
			{
				case PingPayloadPattern.Ones:
					for (int i = 0; i < payload.Length; i++)
						payload[i] = 0xFF;
					break;
				case PingPayloadPattern.Alternating:
					for (int i = 0; i < payload.Length; i++)
						payload[i] = (i & 1) == 0 ? (byte)0x55 : (byte)0xAA;
					break;
				case PingPayloadPattern.Random:
					random.NextBytes(payload);
					break;
			}
			return payload;
		}

		/// <summary>
		/// Returns true if the data returned in a ping reply is exactly the data that was sent.
		/// </summary>
		public static bool Matches(byte[] sent, byte[] received)
		{
			if (sent == null || received == null || sent.Length != received.Length)
				return false;
			for (int i = 0; i < sent.Length; i++)
			{
				if (sent[i] != received[i])
					return false;
			}
			return true;
		}
	}
}
