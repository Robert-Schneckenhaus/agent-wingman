namespace Billing.Api;

public static class CatalogApi
{
    public static RouteGroupBuilder MapCatalogApi(this IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("api/catalog").HasApiVersion(1.0);
        api.MapGet("/items/{id:int}", GetItemById);
        api.MapGet("/items", (int pageSize) => Results.Ok());
        return api;
    }

    /// <summary>Returns one catalog item.</summary>
    public static IResult GetItemById(int id) => Results.Ok(id);
}
