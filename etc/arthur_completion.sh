#! /bin/bash

_arthur_completion()
{

    local cur prev cmds
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    # All sub-commands
    cmds="initialize create_user design sync dump load update unload etl validate explain ls ping"
    cmds="$cmds show_dependencies show_pipelines"

    if [ "$prev" = "arthur.py" ]; then
        COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
    elif [ ! -d schemas ]; then
        COMPREPLY=( )
    else
        local SCHEMAS
        case "$cur" in
          *.*)
            SCHEMAS=$(find -L schemas -type f -name '*.yaml' | sed -e 's:schemas/\([^/]*\)/[^-]*-\([^.]*\).yaml:\1.\2:')
            ;;
          *)
            SCHEMAS=$(find -L schemas -type f -name '*.yaml' | sed -e 's:schemas/\([^/]*\)/[^-]*-\([^.]*\).yaml:\1:' | uniq)
            ;;
        esac
        COMPREPLY=( $(compgen -W "$SCHEMAS" -- "$cur") )
    fi

}

complete -F _arthur_completion arthur.py
