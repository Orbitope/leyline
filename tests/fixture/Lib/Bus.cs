using System;

namespace Lib
{
    public class Bus
    {
        public event Action<int> Changed;
        public void Set(int v) { Changed?.Invoke(v); }
    }
}
