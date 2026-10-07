package com.acme;

public class Counter {
    private int count;

    public Counter() { this(0); }
    public Counter(int start) { count = start; }

    public void add(int n) { count += n; }
    public int get() { return count; }
}
