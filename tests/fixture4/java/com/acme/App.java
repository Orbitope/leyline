package com.acme;

public class App {
    public static void main(String[] args) {
        Counter c = new Counter(5);
        c.add(2);
        System.out.println(c.get());
    }
}
