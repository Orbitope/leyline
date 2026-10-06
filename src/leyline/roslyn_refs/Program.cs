// Reads a manifest of C# modules, binds each with the compiler, and prints one JSON line per
// call or field access that the compiler resolved to a declaration in the same workspace.
//
//   dotnet run -- manifest.json > refs.jsonl
//
// manifest: {"root": "...", "modules": [{"name": "...", "files": ["rel/path.cs"], "refs": ["other module"]}]}
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

var manifest = JsonDocument.Parse(File.ReadAllText(args[0])).RootElement;
var root = manifest.GetProperty("root").GetString();
var modules = manifest.GetProperty("modules").EnumerateArray().ToList();

// Framework reference assemblies: the newest pack under the running SDK's root.
var dotnetRoot = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(typeof(object).Assembly.Location), "..", "..", ".."));
var packs = Path.Combine(dotnetRoot, "packs", "Microsoft.NETCore.App.Ref");
var refDir = Directory.Exists(packs)
    ? Directory.GetDirectories(packs).OrderBy(d => d).Select(d => Directory.GetDirectories(Path.Combine(d, "ref")).FirstOrDefault()).LastOrDefault(d => d != null)
    : Path.GetDirectoryName(typeof(object).Assembly.Location);
var framework = Directory.GetFiles(refDir, "*.dll").Select(f => (MetadataReference)MetadataReference.CreateFromFile(f)).ToList();

var built = new Dictionary<string, CSharpCompilation>();
var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = false };
var pending = modules.ToList();
while (pending.Count > 0)
{
    // A module is ready when everything it references is built; a cycle is broken by taking the first.
    var next = pending.FirstOrDefault(m => m.GetProperty("refs").EnumerateArray().All(r => built.ContainsKey(r.GetString()) || !modules.Any(x => x.GetProperty("name").GetString() == r.GetString())));
    if (next.ValueKind == JsonValueKind.Undefined) next = pending[0];
    pending.Remove(next);
    var name = next.GetProperty("name").GetString();
    var trees = new List<SyntaxTree>();
    var options = new CSharpParseOptions(LanguageVersion.Preview);
    foreach (var f in next.GetProperty("files").EnumerateArray())
    {
        var rel = f.GetString();
        var path = Path.Combine(root, rel);
        if (File.Exists(path)) trees.Add(CSharpSyntaxTree.ParseText(File.ReadAllText(path), options, rel));
    }
    // Projects that use implicit usings compile against these; adding them never hides a real error we care about.
    trees.Add(CSharpSyntaxTree.ParseText("global using System; global using System.Collections.Generic; global using System.IO; global using System.Linq; global using System.Threading; global using System.Threading.Tasks;", options, "<implicit>"));
    var refs = new List<MetadataReference>(framework);
    foreach (var r in next.GetProperty("refs").EnumerateArray())
        if (built.TryGetValue(r.GetString(), out var dep)) refs.Add(dep.ToMetadataReference());
    var compilation = CSharpCompilation.Create(name, trees, refs,
        new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary, allowUnsafe: true, nullableContextOptions: NullableContextOptions.Enable));
    built[name] = compilation;

    int ok = 0, candidate = 0, none = 0, outside = 0;
    foreach (var tree in trees)
    {
        if (tree.FilePath == "<implicit>") continue;
        var model = compilation.GetSemanticModel(tree);
        foreach (var node in tree.GetRoot().DescendantNodes())
        {
            switch (node)
            {
                case InvocationExpressionSyntax inv:
                {
                    var nameNode = inv.Expression switch
                    {
                        MemberAccessExpressionSyntax m => (SyntaxNode)m.Name,
                        MemberBindingExpressionSyntax b => b.Name,
                        _ => inv.Expression,
                    };
                    var written = nameNode is SimpleNameSyntax s ? s.Identifier.ValueText : null;
                    if (written == null || written == "nameof") break;
                    EmitCall(model.GetSymbolInfo(inv), written, nameNode.GetLocation(), ref ok, ref candidate, ref none, ref outside);
                    break;
                }
                case BaseObjectCreationExpressionSyntax made:
                    EmitCall(model.GetSymbolInfo(made), ".ctor", made.GetLocation(), ref ok, ref candidate, ref none, ref outside);
                    break;
                case ConstructorInitializerSyntax init:
                    EmitCall(model.GetSymbolInfo(init), ".ctor", init.GetLocation(), ref ok, ref candidate, ref none, ref outside);
                    break;
                case IdentifierNameSyntax id:
                {
                    if (id.Parent is InvocationExpressionSyntax call && call.Expression == id) break;
                    if (id.Parent is MemberAccessExpressionSyntax ma && ma.Name == id && ma.Parent is InvocationExpressionSyntax) break;
                    var symbol = model.GetSymbolInfo(id).Symbol;
                    if (symbol is not (IFieldSymbol or IPropertySymbol)) break;
                    if (symbol is IFieldSymbol { ContainingType.TypeKind: TypeKind.Enum }) break;
                    var decl = Declared(symbol);
                    if (decl == null) break;
                    Emit(Access(id), id.GetLocation(), symbol.Name, decl.Value.file, decl.Value.line, "ok");
                    break;
                }
            }
        }
    }
    // Per file: how many binding errors, so a reader knows whether "nothing found" means "nothing there".
    foreach (var tree in trees)
    {
        if (tree.FilePath == "<implicit>") continue;
        var errors = compilation.GetSemanticModel(tree).GetDiagnostics().Count(d => d.Severity == DiagnosticSeverity.Error);
        stdout.WriteLine("{\"k\":\"file\",\"f\":" + JsonSerializer.Serialize(tree.FilePath) + ",\"e\":" + errors + "}");
    }
    Console.Error.WriteLine($"{name}: {trees.Count - 1} files, {ok} calls bound, {candidate} by candidate, {none} unbound, {outside} to code outside");
}
stdout.Flush();

void EmitCall(SymbolInfo info, string written, Location at, ref int ok, ref int candidate, ref int none, ref int outside)
{
    var symbol = info.Symbol as IMethodSymbol;
    var state = "ok";
    if (symbol == null && info.CandidateSymbols.Length == 1) { symbol = info.CandidateSymbols[0] as IMethodSymbol; state = "candidate"; }
    if (symbol == null)
    {
        // Several candidates, or none: the compiler could not settle it (often a missing package). Say so.
        none++;
        Emit("call", at, written, null, 0, info.CandidateSymbols.Length > 1 ? "ambiguous" : "none");
        return;
    }
    if (symbol.MethodKind == MethodKind.LocalFunction || symbol.MethodKind == MethodKind.Ordinary || symbol.MethodKind == MethodKind.Constructor
        || symbol.MethodKind == MethodKind.ReducedExtension || symbol.MethodKind == MethodKind.UserDefinedOperator || symbol.MethodKind == MethodKind.DelegateInvoke)
    {
        var target = (symbol.ReducedFrom ?? symbol).OriginalDefinition;
        if (target.PartialImplementationPart != null) target = target.PartialImplementationPart;
        var decl = Declared(target);
        if (decl == null) { outside++; Emit("call", at, written, null, 0, "outside"); return; }
        if (state == "ok") ok++; else candidate++;
        Emit("call", at, written, decl.Value.file, decl.Value.line, state, target.MethodKind == MethodKind.Constructor ? ".ctor" : target.Name);
    }
}

(string file, int line)? Declared(ISymbol symbol)
{
    foreach (var r in symbol.OriginalDefinition.DeclaringSyntaxReferences)
    {
        if (r.SyntaxTree.FilePath == "<implicit>") continue;
        var syntax = r.GetSyntax();
        // A field is declared inside a declaration that may hold several: point at the whole statement.
        if (syntax is VariableDeclaratorSyntax && syntax.Parent?.Parent is BaseFieldDeclarationSyntax whole) syntax = whole;
        return (r.SyntaxTree.FilePath, syntax.GetLocation().GetLineSpan().StartLinePosition.Line + 1);
    }
    return null;
}

string Access(IdentifierNameSyntax id)
{
    SyntaxNode cur = id;
    if (cur.Parent is MemberAccessExpressionSyntax m && m.Name == id) cur = m;
    while (cur.Parent is ParenthesizedExpressionSyntax p) cur = p;
    var up = cur.Parent;
    if (up is AssignmentExpressionSyntax a && a.Left == cur)
    {
        if (a.Parent is InitializerExpressionSyntax) return "init";
        return a.IsKind(SyntaxKind.SimpleAssignmentExpression) ? "write" : "readwrite";
    }
    if (up is PostfixUnaryExpressionSyntax || up is PrefixUnaryExpressionSyntax pre && (pre.IsKind(SyntaxKind.PreIncrementExpression) || pre.IsKind(SyntaxKind.PreDecrementExpression)))
        return up is PostfixUnaryExpressionSyntax post && post.IsKind(SyntaxKind.SuppressNullableWarningExpression) ? "read" : "readwrite";
    if (up is ArgumentSyntax arg && !arg.RefKindKeyword.IsKind(SyntaxKind.None))
        return arg.RefKindKeyword.IsKind(SyntaxKind.OutKeyword) ? "write" : arg.RefKindKeyword.IsKind(SyntaxKind.RefKeyword) ? "readwrite" : "read";
    if (up is ElementAccessExpressionSyntax e && e.Expression == cur && e.Parent is AssignmentExpressionSyntax ea && ea.Left == e) return "readwrite";
    return "read";
}

void Emit(string kind, Location at, string written, string targetFile, int targetLine, string state, string targetName = null)
{
    var pos = at.GetLineSpan();
    var sb = new StringBuilder(160);
    sb.Append("{\"k\":\"").Append(kind).Append("\",\"f\":").Append(JsonSerializer.Serialize(pos.Path))
      .Append(",\"l\":").Append(pos.StartLinePosition.Line + 1).Append(",\"n\":").Append(JsonSerializer.Serialize(written))
      .Append(",\"s\":\"").Append(state).Append('"');
    if (targetFile != null) sb.Append(",\"tf\":").Append(JsonSerializer.Serialize(targetFile)).Append(",\"tl\":").Append(targetLine)
        .Append(",\"tn\":").Append(JsonSerializer.Serialize(targetName ?? written));
    sb.Append('}');
    stdout.WriteLine(sb.ToString());
}
