using Orders;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddSingleton<OrderService>();
var app = builder.Build();
app.MapGet("/orders/{reference}", (string reference, OrderService service) =>
    Results.Ok(new { Reference = service.NormalizeReference(reference) }));
app.Run();
