🐕container-watchdog

i kept finding out my containers had crashed hours ago and i had no idea. so i built this python project over a weekend and honestly it's one of those tools i use every single day without thinking about it.

it just watches my docker host and texts me on telegram when stuff happens. container dies, runs out of memory, health check goes red — i get a message. that's it. no dashboards, no grafana setup, just a telegram ping at 2am saying "hey your container just died lol"

the whole thing runs in a tiny alpine container on the same host. set it and forget it.