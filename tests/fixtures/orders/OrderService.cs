namespace Orders;

public sealed class OrderService
{
    public string NormalizeReference(string reference)
    {
        return reference.Trim().ToUpperInvariant();
    }
}
