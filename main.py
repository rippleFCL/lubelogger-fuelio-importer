"""Script to import Fuelio fillups into Lubelogger"""

import argparse
import csv
import logging
import tempfile
import zipfile
from datetime import datetime
from os import path
from textwrap import dedent

import yaml
from pydrive2.files import GoogleDriveFile

import gdrive
from lubelogger import Lubelogger, LubeloggerFillup

logger = logging.getLogger(__name__)

def load_config() -> dict:
    """Loads config from YAML file"""
    config_path = path.join(path.dirname(__file__), "config.yml")
    with open(config_path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def fuelio_csv_from_backup(backup: GoogleDriveFile, filename: str) -> csv.DictReader:
    """Returns Fuelio data from Google Drive backup"""
    with tempfile.TemporaryDirectory() as tempdir:
        backup_path = path.join(tempdir, "fuelio.zip")
        extract_path = path.join(tempdir, "fuelio")

        backup.GetContentFile(backup_path, mimetype="application/zip")
        with zipfile.ZipFile(backup_path, "r") as zip_ref:
            zip_ref.extractall(extract_path)

        return csv.DictReader(
            open(path.join(extract_path, filename), "r", encoding="utf-8")
        )


def filter_fuelio_fillups(fuelio_data: csv.DictReader) -> list[dict]:
    """Filters Fuelio CSV export to only include fuel fillups"""
    fillups = []
    for fillup in fuelio_data:
        try:
            datetime.strptime(fillup.get("## Vehicle"), "%Y-%m-%d %H:%M")
            fillups.append(fillup)
        except ValueError:
            pass

    return fillups


def lubelogger_converter(fillup) -> LubeloggerFillup:
    """Converts a Fuelio fillup to Lubelogger fillup"""
    fillup_datetime = datetime.strptime(fillup["## Vehicle"], "%Y-%m-%d %H:%M")
    fillup_notes = dedent(
        f"""
            Fuel station: {fillup[None][7].strip()}

            Location: [{fillup[None][5]},{fillup[None][6]}](https://www.google.com/maps/place/{fillup[None][5]},{fillup[None][6]})

            Time: {fillup_datetime.strftime('%H:%M')}"""
    ).strip()

    if fillup[None][8]:
        fillup_notes += f"\n\n###### Fuelio notes:\n\n{fillup[None][8]}"

    return LubeloggerFillup(
        date            = fillup_datetime.strftime('%d/%m/%Y'),
        odometer        = int(float(fillup[None][0])),
        fuel_consumed   = fillup[None][1],
        cost            = fillup[None][3],
        is_fill_to_full = int(fillup[None][2]) == 1,
        missed_fuel_up  = int(fillup[None][9]) == 1,
        notes           = fillup_notes
    )


def fetch_backup_data(config: dict) -> list[dict]:
    """Fetches Fuelio backup data"""
    folder_id = config["drive_folder_id"]
    vehicle_id = config["fuelio_vehicle_id"]
    fuelio_csv_filename = f"vehicle-{vehicle_id}-sync.csv"

    assert config["auth_type"] in gdrive.AuthType, "Invalid auth_type"
    drive = gdrive.GDrive(auth_type=gdrive.AuthType[str(config["auth_type"]).upper()])

    backup = drive.find_file(folder_id, fuelio_csv_filename + ".zip")[0]

    assert len(backup) > 0, f"No backup found for {vehicle_id}"

    fuelio_data = fuelio_csv_from_backup(backup, fuelio_csv_filename)
    fuelio_fills = filter_fuelio_fillups(fuelio_data)

    return fuelio_fills



def process_fillups(
    fuelio_fills: list[dict],
    lubelogger: Lubelogger,
    lubelog_fills: set[LubeloggerFillup],
    config: dict,
    dry_run: bool,
):
    """Processes fillups"""

    unique_fuelio_fills = set(map(lubelogger_converter, fuelio_fills))

    exiting_duplicate_ll_fills = lubelog_fills.union(unique_fuelio_fills)
    new_duplicate_ll_fills = unique_fuelio_fills.union(lubelog_fills)

    new_ll_fills = unique_fuelio_fills.union(
        lubelog_fills.difference(unique_fuelio_fills)
    )

    # Loop through the fuelio fills list in reverse order (oldest first)
    for new_fill in new_ll_fills:
        if not dry_run:
            lubelogger.add_fillup(config["lubelogger_vehicle_id"], new_fill)
        else:
            logger.info("Dry run: Would add fuel fillup from %s", new_fill.date)

    for existing_duplicate_fill, new_duplicate_fill in zip(exiting_duplicate_ll_fills, new_duplicate_ll_fills):
        logger.warning(
            "Found existing fillup on %s with different attributes.",
            new_fill.date,
        )
        logger.warning(
            "This is likely a duplicate and the following"
            + "attributes will need to be manually patched:"
        )

        logger.debug("Existing fill: %s", existing_duplicate_fill.as_dict)
        logger.debug("Incoming fill: %s", new_duplicate_fill.as_dict)

        # Log each key/value pair that does not match
        for key, value in new_duplicate_fill:
            if value != existing_duplicate_fill[key]:
                logger.warning("%s: %s -> %s", key, existing_duplicate_fill[key], value)
    if not new_ll_fills:
        logger.info("Nothing to add, Lubelogger fuel logs are up to date!")


def main(args):
    logger = logging.getLogger(__name__)
    logsh = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    logsh.setFormatter(formatter)
    logger.addHandler(logsh)

    config = load_config()

    if config.get("debug") or str(config.get("log_level")).lower() == "debug":
        logger.setLevel(logging.DEBUG)

    lubelogger = Lubelogger(
        config["lubelogger_url"],
        config["lubelogger_username"],
        config["lubelogger_password"],
    )

    fuelio_fills = fetch_backup_data(config)

    logger.debug("Found %d fillups in Fuelio backup", len(list(fuelio_fills)))
    if len(fuelio_fills) == 0:
        logger.info("No fuel fillups found in Fuelio backup")
        return

    lubelog_fills = lubelogger.get_fillups(config["lubelogger_vehicle_id"])

    logger.debug("Found %d fillups in Lubelogger", len(lubelog_fills))

    process_fillups(fuelio_fills, lubelogger, lubelog_fills, config, args.dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import Fuelio fillups into Lubelogger"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without making any changes",
    )
    args = parser.parse_args()
    main(args)
