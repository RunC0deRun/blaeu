# blaeu

A GPX cartographer

## Goals
1. Uploading
    - The app should allow uploading GPX files.
    - The app should show a static map with the route.
    - The app should show basic statistics about the route:
        - Total distance
        - Total elevation gain
        - Total elevation loss
        - Duration
        - Average speed (total)
        - Average moving speed (excluding pauses)
        - Maximum speed
        - Number of waypoints
        - Number of tracks
        - Number of track segments
        - Number of track points
2. Animating
    - The app should allow animating the route.
    - The app should allow controling the animation (play, pause, stop, speed control).
    - The app should allow exporting the animation as a video (initially WebM).
3. Organizing
    - The app should allow organizing routes in folders.
    - The app should allow tagging routes with keywords.
    - The app should list routes in a timeline from newest to oldest.
    - The app should allow deleting routes.
    - The app should allow editing route names and descriptions.
4. Future Extensions
    - Support exporting the animation as a video in MP4 format.

## UX Requirements
- The UI should be minimalistic and clean.
- Yet the UI should have nods to cartography of old. Especially to its namesake Joan Blaeu (https://en.wikipedia.org/wiki/Joan_Blaeu)
- Animations should feel like the are drawn on the map.

## Requirements
1. The app needs to be run in a standalone docker container.
2. Any persistence should be stored on a volume mounted to the container.

## Tests
For Goal #1
- Upload a gpx file with a single track and verify the statistics are correct.
- Upload a gpx file with multiple tracks and verify the statistics are correct.
- Upload a duplicate gpx file and verify that it is not counted as a new file.
For Goal #2
- Upload a gpx file and verify that the animation is correct.
- Upload a gpx file and verify that the animation can be exported as a video.

## Development Practices
- Work through the goals in order and ensure that each goal is fully implemented before moving on to the next.
- Ask to commit significant changes to the codebase and provide a summary of the changes.
- Write tests for new functionality and verify that existing tests still pass.
- Write any important notes about the implementation of this application in AGENT.md.
