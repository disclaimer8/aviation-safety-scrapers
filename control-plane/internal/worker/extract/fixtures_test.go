package extract

import "context"

// fixtureOCRClient is the offline OCRClient for tests.
type fixtureOCRClient struct {
	Text string
	Err  error
}

func (f *fixtureOCRClient) OCR(ctx context.Context, pdf []byte) (string, error) {
	if f.Err != nil {
		return "", f.Err
	}
	return f.Text, nil
}

var _ OCRClient = (*fixtureOCRClient)(nil)

// fixtureLLMClient is the offline LLMClient for tests.
type fixtureLLMClient struct {
	Event ExtractedEvent
	Err   error
}

func (f *fixtureLLMClient) Extract(ctx context.Context, text string) (ExtractedEvent, error) {
	if f.Err != nil {
		return ExtractedEvent{}, f.Err
	}
	return f.Event, nil
}

var _ LLMClient = (*fixtureLLMClient)(nil)
