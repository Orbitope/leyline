using System;

namespace Mod.Boxes;

public interface IBox { int Size(); }

public class Box : IBox
{
    public virtual int Size() => 1;
}

public class Box<T> : Box
{
    public override int Size() => 2;
}

public class Crate : Box<string> { }
