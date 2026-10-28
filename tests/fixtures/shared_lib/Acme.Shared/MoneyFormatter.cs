namespace Acme.Shared;

/// <summary>Formats money values for documents.</summary>
public static class MoneyFormatter
{
    public static string Format(decimal value) => value.ToString("0.00");
}
