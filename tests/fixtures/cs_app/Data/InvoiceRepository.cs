using Billing.Models;

namespace Billing.Data;

public interface IUnitOfWork
{
    Task<int> SaveEntitiesAsync(CancellationToken cancellationToken = default);
}

public interface IInvoiceRepository
{
    IUnitOfWork UnitOfWork { get; }
    Task<Invoice> GetAsync(int id);
}

public class BillingContext : IUnitOfWork
{
    public Task<int> SaveEntitiesAsync(CancellationToken cancellationToken = default)
        => Task.FromResult(1);
}

public class InvoiceRepository : IInvoiceRepository
{
    private readonly BillingContext _context = new();

    public IUnitOfWork UnitOfWork => _context;

    public Task<Invoice> GetAsync(int id) => Task.FromResult(new Invoice());
}

public class CloseInvoiceHandler
{
    private readonly IInvoiceRepository _repository;

    public CloseInvoiceHandler(IInvoiceRepository repository) => _repository = repository;

    protected IInvoiceRepository Repository => _repository;

    public async Task Handle(int id)
    {
        var invoice = await _repository.GetAsync(id);
        await _repository.UnitOfWork.SaveEntitiesAsync();
        await Repository.UnitOfWork.SaveEntitiesAsync();
    }
}
