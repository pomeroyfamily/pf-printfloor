# Third-party data

## Bambu error texts (`app/drivers/bambu/hms_en.json.gz`)
From ha-bambulab (https://github.com/greghesp/ha-bambulab), `custom_components/bambu_lab/pybambu/hms_error_text/hms_en.json.gz`, copied 2026-11-02. License, verbatim:

```
MIT License

Copyright (c) 2023 ha-bambulab contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

# Python packages

Not part of this repository: installed from PyPI (`requirements.txt`) and included in the Docker image. Each keeps its
own license.

| Package | License | Why |
|---|---|---|
| fastapi, starlette | MIT, BSD-3-Clause | web server and API |
| uvicorn | BSD-3-Clause | runs the web server |
| pydantic | MIT | data models |
| paho-mqtt | EPL-2.0 or BSD-3-Clause | Bambu MQTT |
| apprise | BSD-2-Clause | sending notifications |
| requests, requests-oauthlib, urllib3, certifi | Apache-2.0, ISC, MIT, MPL-2.0 | used by apprise |
| markdown, PyYAML, click | BSD-3-Clause, MIT, BSD-3-Clause | used by apprise |
| av (PyAV) | BSD-3-Clause; its wheel bundles FFmpeg (LGPL/GPL parts, incl. x264/x265) and other codec libraries under their own licenses | X1C camera: decoding the H.264 stream |
