# Go orchestrator: one static binary (worker | gateway | migrate | schedule) on distroless.
# Build context: repo root (needs companies.yaml).
FROM golang:1.25-alpine AS build
WORKDIR /src/orchestrator
COPY orchestrator/go.mod orchestrator/go.sum ./
RUN go mod download
COPY orchestrator/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/jobradar ./cmd/jobradar

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/jobradar /jobradar
COPY companies.yaml /etc/jobradar/companies.yaml
ENV COMPANIES_FILE=/etc/jobradar/companies.yaml
USER nonroot
ENTRYPOINT ["/jobradar"]
