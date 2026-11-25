DOMAIN = "grenton"

# Name of the myGrenton dashboard page whose VALUE_V2 widgets are exposed as
# gesture `event` entities instead of `sensor` entities. Compared case-insensitively
# after trimming whitespace.
EVENT_PAGE_NAME = "HA Events"

# Event fired on the Home Assistant bus after a CLU restart was detected and
# its state was read again. See README, "CLU restart signal".
EVENT_CLU_RESTARTED = "grenton_clu_restarted"
