import sys
from util import Report, load


def main():
    rows = load(sys.argv[1])
    report = Report(rows)
    print(report.total())


if __name__ == "__main__":
    main()
