FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN CGO_ENABLED=0 go build -o /cachecron .

FROM alpine:3.20
COPY --from=build /cachecron /cachecron
EXPOSE 9000
ENTRYPOINT ["/cachecron"]
