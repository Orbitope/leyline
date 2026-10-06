using System.Threading.Tasks;

namespace Mod.Fluent;

public class Plan
{
    public Step First() => new Step();
    public Task<Step> FirstAsync() => Task.FromResult(new Step());
}

public class Step
{
    public Step Then() => this;
    public void Done() { }
}

public static class StepExtensions
{
    public static Step Twice(this Step step) => step.Then().Then();
}

public static class Use
{
    public static async Task Go(Plan plan)
    {
        plan.First().Then().Done();
        var step = plan.First();
        step.Twice();
        var later = await plan.FirstAsync();
        later.Done();
    }
}
