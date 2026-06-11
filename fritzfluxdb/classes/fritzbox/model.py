#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/model.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from fritzfluxdb.log import get_logger

log = get_logger()


class FritzBoxLinkTypes:
    Fiber = "Fiber"
    Cable = "Cable"
    DSL = "DSL"
    NO_MODEM = "NoModem"
    Mobile = "Mobile"
    Ethernet = "Ethernet"
    Other = "Other"


class FritzBoxModel:

    # Checked first — mixed/ambiguous models that cannot be classified by number alone
    _special_model_link_types: dict[str, str] = {
        "5690 Pro": FritzBoxLinkTypes.Other,   # DSL + Fiber
        "5550": FritzBoxLinkTypes.Other,        # DSL + Fiber / mixed
    }

    # Fallback table when TR-064 does not report a link type.
    # Longer/more-specific strings must appear before shorter ones so that
    # "5690 XGS" is matched before the generic "5690" entry.
    _fritzbox_model_link_types: dict[str, str] = {
        # no-modem / network routers
        "4020": FritzBoxLinkTypes.NO_MODEM,
        "4040": FritzBoxLinkTypes.NO_MODEM,
        "4050": FritzBoxLinkTypes.NO_MODEM,
        "4060": FritzBoxLinkTypes.NO_MODEM,
        "4630": FritzBoxLinkTypes.NO_MODEM,
        "4690": FritzBoxLinkTypes.NO_MODEM,

        # fiber
        "5490": FritzBoxLinkTypes.Fiber,
        "5491": FritzBoxLinkTypes.Fiber,
        "5530": FritzBoxLinkTypes.Fiber,
        "5590": FritzBoxLinkTypes.Fiber,
        "5690 XGS": FritzBoxLinkTypes.Fiber,   # must come before "5690"
        "5690": FritzBoxLinkTypes.Fiber,

        # cable
        "6340": FritzBoxLinkTypes.Cable,
        "6360": FritzBoxLinkTypes.Cable,
        "6430": FritzBoxLinkTypes.Cable,
        "6490": FritzBoxLinkTypes.Cable,
        "6590": FritzBoxLinkTypes.Cable,
        "6591": FritzBoxLinkTypes.Cable,
        "6660": FritzBoxLinkTypes.Cable,
        "6670": FritzBoxLinkTypes.Cable,
        "6690": FritzBoxLinkTypes.Cable,

        # mobile / LTE / 5G
        "6810": FritzBoxLinkTypes.Mobile,
        "6820": FritzBoxLinkTypes.Mobile,
        "6825": FritzBoxLinkTypes.Mobile,
        "6840": FritzBoxLinkTypes.Mobile,
        "6842": FritzBoxLinkTypes.Mobile,
        "6850": FritzBoxLinkTypes.Mobile,
        "6860": FritzBoxLinkTypes.Mobile,
        "6890": FritzBoxLinkTypes.Mobile,   # mixed LTE + DSL, Mobile as fallback

        # DSL / G.fast
        "3272": FritzBoxLinkTypes.DSL,
        "3370": FritzBoxLinkTypes.DSL,
        "3390": FritzBoxLinkTypes.DSL,
        "3490": FritzBoxLinkTypes.DSL,
        "7272": FritzBoxLinkTypes.DSL,
        "7312": FritzBoxLinkTypes.DSL,
        "7320": FritzBoxLinkTypes.DSL,
        "7330": FritzBoxLinkTypes.DSL,
        "7340": FritzBoxLinkTypes.DSL,
        "7360": FritzBoxLinkTypes.DSL,
        "7362": FritzBoxLinkTypes.DSL,
        "7369": FritzBoxLinkTypes.DSL,
        "7390": FritzBoxLinkTypes.DSL,
        "7412": FritzBoxLinkTypes.DSL,
        "7430": FritzBoxLinkTypes.DSL,
        "7490": FritzBoxLinkTypes.DSL,
        "7510": FritzBoxLinkTypes.DSL,
        "7520": FritzBoxLinkTypes.DSL,
        "7530": FritzBoxLinkTypes.DSL,
        "7560": FritzBoxLinkTypes.DSL,
        "7580": FritzBoxLinkTypes.DSL,
        "7581": FritzBoxLinkTypes.DSL,
        "7582": FritzBoxLinkTypes.DSL,
        "7583": FritzBoxLinkTypes.DSL,
        "7590": FritzBoxLinkTypes.DSL,
        "7630": FritzBoxLinkTypes.DSL,
        "7682": FritzBoxLinkTypes.DSL,   # G.fast
        "7690": FritzBoxLinkTypes.DSL,
    }

    @classmethod
    def get_link_type(cls, model_name, discovered_link_mode):

        if discovered_link_mode is not None and discovered_link_mode != FritzBoxLinkTypes.Other:
            if discovered_link_mode not in FritzBoxLinkTypes.__dict__.values():
                log.info(f"Unknown FritzBox link type '{discovered_link_mode}'. "
                         f"Please report this link type as issue to the github_project")
            return discovered_link_mode

        if not model_name:
            return FritzBoxLinkTypes.Other

        # Check ambiguous/mixed models first (longer strings take precedence)
        for special_model, link_type in sorted(
            cls._special_model_link_types.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if special_model in model_name:
                return link_type

        # General fallback table — longer keys first to avoid short-key false matches
        for fb_model, link_type in sorted(
            cls._fritzbox_model_link_types.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if fb_model in model_name:
                return link_type

        return FritzBoxLinkTypes.Other

