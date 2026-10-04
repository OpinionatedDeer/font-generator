from pathlib import Path
import yaml

from .font_converter import FontConverter


CONFIG_DIR = Path("config")


def main():
    configs = sorted(CONFIG_DIR.glob("*.yaml"))

    if not configs:
        print("No config files found.")
        return

    print(f"Found {len(configs)} config file(s).\n")

    for config_path in configs:
        print("=" * 60)
        print(f"Config: {config_path}")
        print("=" * 60)

        with open(config_path, "r", encoding="utf-8") as fp:
            config = yaml.safe_load(fp)

        converter = FontConverter(config)
        converter.generate()

        print()


if __name__ == "__main__":
    main()

