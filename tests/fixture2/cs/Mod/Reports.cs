using System.IO;

namespace Mod;

public static class Reports
{
    public static string Load(string name) => File.ReadAllText($"../reports/{name}.parity.json");
}
